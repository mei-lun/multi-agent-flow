"""Workflow 编辑、校验和发布接口。

The service layer deliberately keeps graph validation deterministic.  A graph is
configuration data, so validation must never execute condition expressions or
depend on a model response.
"""

from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import inspect
import json
import re
import uuid
from typing import Protocol
from maf_contracts.common import ActorContext
from maf_domain.errors import (
    ArgumentError,
    IdempotencyConflictError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)
from .repository import InMemoryWorkflowRepository
from .schemas import *


class WorkflowValidator(Protocol):
    def validate(self, graph: WorkflowGraph) -> ValidationReport:
        """执行无副作用静态检查。

        至少检查唯一 start、节点/边 key 唯一、边端点存在、所有节点可达、存在成功结束路径、
        无禁止环、Agent 节点绑定已发布 Role Version、输入输出 Contract 能衔接、条件表达式
        只使用白名单字段和运算符、重试与返工有上限。一次返回全部问题。
        """
        ...


class StaticWorkflowValidator:
    """Pure validator for the persisted workflow graph.

    The validator does not resolve external Role versions.  It only checks that
    an Agent node carries a non-empty exact reference; publication code can then
    resolve that reference in its own transaction.  Returning all findings in a
    stable order makes draft validation and replay reproducible.
    """

    _CONDITION_NAMES = {"status", "result", "approved", "attempt", "retries"}
    _CONDITION_NODES = (
        ast.Expression,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
        ast.Compare,
        ast.Name,
        ast.Load,
        ast.Constant,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.In,
        ast.NotIn,
        ast.Is,
        ast.IsNot,
        ast.List,
        ast.Tuple,
    )
    _CONDITION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\s*(?:==|!=|<=|>=|<|>|in|not in|is|is not)\s*[^;]+)?(?:\s+(?:and|or)\s+.*)?$")

    def validate(self, graph: WorkflowGraph) -> ValidationReport:
        errors: list[dict[str, object]] = []
        warnings: list[dict[str, object]] = []
        nodes = list(graph.get("nodes", []))
        edges = list(graph.get("edges", []))
        start = graph.get("start_node_key")

        def error(code: str, path: str, message: str) -> None:
            errors.append({"code": code, "path": path, "message": message})

        def warning(code: str, path: str, message: str) -> None:
            warnings.append({"code": code, "path": path, "message": message})

        node_keys = [str(node.get("key", "")) for node in nodes]
        edge_keys = [str(edge.get("key", "")) for edge in edges]
        seen: set[str] = set()
        for index, key in enumerate(node_keys):
            if not key:
                error("NODE_KEY_REQUIRED", f"nodes[{index}].key", "node key is required")
            elif key in seen:
                error("DUPLICATE_NODE_KEY", f"nodes[{index}].key", f"duplicate node key: {key}")
            seen.add(key)
        seen_edges: set[str] = set()
        for index, key in enumerate(edge_keys):
            if not key:
                error("EDGE_KEY_REQUIRED", f"edges[{index}].key", "edge key is required")
            elif key in seen_edges:
                error("DUPLICATE_EDGE_KEY", f"edges[{index}].key", f"duplicate edge key: {key}")
            seen_edges.add(key)

        key_set = set(node_keys)
        if not start:
            error("START_REQUIRED", "start_node_key", "start node is required")
        elif start not in key_set:
            error("START_NOT_FOUND", "start_node_key", f"unknown start node: {start}")

        kinds = {str(node.get("key", "")): str(node.get("kind", "")) for node in nodes}
        for index, node in enumerate(nodes):
            key = str(node.get("key", ""))
            kind = str(node.get("kind", ""))
            path = f"nodes[{index}]"
            if kind not in {"AGENT", "GATE", "HUMAN_GATE", "END_SUCCESS", "END_FAILURE"}:
                error("UNKNOWN_NODE_KIND", f"{path}.kind", f"unsupported node kind: {kind}")
            if kind == "AGENT":
                role_version = node.get("role_version_id")
                if not isinstance(role_version, str) or not role_version.strip():
                    error("ROLE_VERSION_REQUIRED", f"{path}.role_version_id", "Agent node needs a published role version")
                role_status = node.get("role_version_status")
                if role_status is not None and role_status != "PUBLISHED":
                    error("ROLE_VERSION_NOT_PUBLISHED", f"{path}.role_version_status", "Agent node must bind a published role version")
            retry = node.get("retry_policy")
            if retry is None:
                retry = {}
            if not isinstance(retry, dict):
                error("RETRY_POLICY_INVALID", f"{path}.retry_policy", "retry_policy must be an object")
            else:
                for limit_key in ("max_retries", "max_attempts", "max_reworks", "max_rework_count"):
                    if limit_key not in retry:
                        continue
                    limit = retry.get(limit_key)
                    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 10:
                        error("RETRY_LIMIT_INVALID", f"{path}.retry_policy.{limit_key}", f"{limit_key} must be an integer from 0 to 10")
                # Rework policy is intentionally accepted as a separate object
                # so callers can distinguish business rework from technical retry.
                rework_policy = node.get("rework_policy")
                if rework_policy is not None:
                    if not isinstance(rework_policy, dict):
                        error("REWORK_POLICY_INVALID", f"{path}.rework_policy", "rework_policy must be an object")
                    else:
                        max_reworks = rework_policy.get("max_reworks", rework_policy.get("max_attempts"))
                        if not isinstance(max_reworks, int) or isinstance(max_reworks, bool) or not 0 <= max_reworks <= 10:
                            error("REWORK_LIMIT_INVALID", f"{path}.rework_policy.max_reworks", "max_reworks must be an integer from 0 to 10")
            timeout = node.get("timeout_seconds")
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
                error("TIMEOUT_INVALID", f"{path}.timeout_seconds", "timeout_seconds must be positive")

        adjacency: dict[str, list[str]] = {key: [] for key in key_set}
        reverse: dict[str, list[str]] = {key: [] for key in key_set}
        for index, edge in enumerate(edges):
            source = str(edge.get("source_node_key", ""))
            target = str(edge.get("target_node_key", ""))
            path = f"edges[{index}]"
            if source not in key_set:
                error("EDGE_SOURCE_NOT_FOUND", f"{path}.source_node_key", f"unknown source node: {source}")
            if target not in key_set:
                error("EDGE_TARGET_NOT_FOUND", f"{path}.target_node_key", f"unknown target node: {target}")
            if source in key_set and target in key_set:
                adjacency[source].append(target)
                reverse[target].append(source)
            condition = edge.get("condition")
            if condition:
                self._validate_condition(str(condition), f"{path}.condition", error)

        self._validate_contracts(nodes, edges, key_set, error)

        reachable: set[str] = set()
        if start in key_set:
            stack = [str(start)]
            while stack:
                key = stack.pop()
                if key in reachable:
                    continue
                reachable.add(key)
                stack.extend(reversed(adjacency.get(key, [])))
        for key in sorted(key_set - reachable):
            error("NODE_UNREACHABLE", f"nodes[{node_keys.index(key)}]", f"node is unreachable: {key}")

        terminals = {key for key, kind in kinds.items() if kind in {"END_SUCCESS", "END_FAILURE"}}
        if not terminals:
            error("END_NODE_REQUIRED", "nodes", "at least one end node is required")
        for key in sorted(key_set):
            if key in terminals:
                continue
            if not adjacency.get(key):
                error("DEAD_END", f"nodes[{node_keys.index(key)}]", f"node has no outgoing edge: {key}")

        # A reverse traversal identifies nodes that can never reach a terminal.
        can_finish: set[str] = set(terminals)
        stack = list(terminals)
        while stack:
            key = stack.pop()
            for parent in reverse.get(key, []):
                if parent not in can_finish:
                    can_finish.add(parent)
                    stack.append(parent)
        for key in sorted(reachable - can_finish):
            error("NO_END_PATH", f"nodes[{node_keys.index(key)}]", f"node cannot reach an end node: {key}")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                error("GRAPH_CYCLE", f"nodes[{node_keys.index(key)}]", f"cycle detected at node: {key}")
                return
            if key in visited:
                return
            visiting.add(key)
            for child in adjacency.get(key, []):
                visit(child)
            visiting.remove(key)
            visited.add(key)

        for key in sorted(key_set):
            visit(key)

        if any(not edge.get("condition") for edge in edges) and len(edges) > len(nodes) - 1:
            warning("UNCONDITIONAL_BRANCH", "edges", "multiple unconditional branches may be ambiguous")

        errors.sort(key=lambda item: (str(item["path"]), str(item["code"]), str(item["message"])))
        warnings.sort(key=lambda item: (str(item["path"]), str(item["code"]), str(item["message"])))
        return {
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "reachable_node_keys": sorted(reachable),
        }

    def _validate_condition(self, expression: str, path: str, error) -> None:
        if len(expression) > 256 or "(" in expression or ")" in expression or "__" in expression:
            error("CONDITION_UNSAFE", path, "condition uses a forbidden expression")
            return
        if not self._CONDITION_RE.fullmatch(expression.strip()):
            error("CONDITION_SYNTAX", path, "condition is not in the restricted expression grammar")
            return
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError:
            error("CONDITION_SYNTAX", path, "condition is not valid syntax")
            return
        for node in ast.walk(tree):
            if not isinstance(node, self._CONDITION_NODES):
                error("CONDITION_UNSAFE", path, "condition contains a forbidden expression")
                return
            if isinstance(node, ast.Name) and node.id not in self._CONDITION_NAMES:
                error("CONDITION_NAME", path, f"condition name is not allowed: {node.id}")
                return
            if isinstance(node, ast.Constant) and not isinstance(node.value, (str, int, float, bool, type(None))):
                error("CONDITION_UNSAFE", path, "condition literal type is not allowed")
                return

    @staticmethod
    def _contract_identity(contract: object) -> tuple[str, str] | None:
        """Return a stable identity for the small contract references in a graph.

        Workflow drafts use a few equivalent spellings (``key``, ``name``,
        ``artifact_type`` and ``schema_id``).  Normalizing these at validation
        time keeps the check deterministic without reaching into Role or
        Artifact repositories.
        """
        if isinstance(contract, str):
            value = contract.strip()
            return (value, "") if value else None
        if not isinstance(contract, dict):
            return None
        identity = next(
            (contract.get(field) for field in ("contract_id", "schema_id", "artifact_type", "key", "name", "type") if contract.get(field)),
            None,
        )
        if not isinstance(identity, str) or not identity.strip():
            return None
        version = contract.get("schema_version", contract.get("version", ""))
        return identity.strip(), str(version).strip()

    def _validate_contracts(self, nodes: list[object], edges: list[object], key_set: set[str], error) -> None:
        node_map = {str(node.get("key", "")): node for node in nodes if isinstance(node, dict)}
        outgoing: dict[str, list[str]] = {key: [] for key in key_set}
        incoming: dict[str, list[str]] = {key: [] for key in key_set}
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            source, target = str(edge.get("source_node_key", "")), str(edge.get("target_node_key", ""))
            if source in key_set and target in key_set:
                outgoing[source].append(target)
                incoming[target].append(source)

        outputs: dict[str, set[tuple[str, str]]] = {}
        inputs: dict[str, list[tuple[str, str]]] = {}
        for key, node in node_map.items():
            for direction, field, target in (("output", "output_contracts", outputs), ("input", "input_contracts", inputs)):
                contracts = node.get(field, [])
                if contracts is None:
                    contracts = []
                if not isinstance(contracts, list):
                    error("CONTRACT_INVALID", f"nodes[{next(i for i, item in enumerate(nodes) if item is node)}].{field}", f"{field} must be a list")
                    continue
                normalized: list[tuple[str, str]] = []
                for contract_index, contract in enumerate(contracts):
                    identity = self._contract_identity(contract)
                    if identity is None:
                        error("CONTRACT_INVALID", f"nodes[{next(i for i, item in enumerate(nodes) if item is node)}].{field}[{contract_index}]", "contract must contain a non-empty identity")
                    elif direction == "output":
                        target.setdefault(key, set()).add(identity)
                    else:
                        normalized.append(identity)
                if direction == "input":
                    target[key] = normalized

        for target, required in inputs.items():
            if not required or not incoming.get(target):
                continue
            available = set().union(*(outputs.get(source, set()) for source in incoming[target]))
            for contract in required:
                if contract not in available and (contract[0], "") not in available:
                    error("CONTRACT_MISMATCH", f"nodes[{next(i for i, item in enumerate(nodes) if item.get('key') == target)}].input_contracts", f"input contract is not produced upstream: {contract[0]}")

        # A node with no outgoing edge is handled by the graph checks above;
        # retaining this map makes the intended contract direction explicit and
        # avoids accidentally treating an input contract as an output.
        _ = outgoing


class WorkflowService(Protocol):
    async def create_workflow(self, actor: ActorContext, request: CreateWorkflowRequest) -> WorkflowView:
        """创建稳定 Workflow Definition；尚未包含可执行 Graph。"""
        ...
    async def create_version(self, actor: ActorContext, workflow_id: str, request: CreateWorkflowVersionRequest) -> WorkflowVersionView:
        """创建 DRAFT；可从已有版本复制，但新旧版本随后完全独立。"""
        ...
    async def save_graph(self, actor: ActorContext, version_id: str, request: SaveGraphRequest) -> WorkflowVersionView:
        """按 expected_version 保存完整 Graph 草稿。

        先做结构解析，再计算规范化 hash；允许保存有校验错误的 DRAFT，但状态标记 FAIL。
        PUBLISHED 版本拒绝修改。
        """
        ...
    async def validate_version(self, actor: ActorContext, version_id: str) -> ValidationReport:
        """读取已保存 Graph，运行 Validator 并持久化报告，不改变发布状态。"""
        ...
    async def publish(self, actor: ActorContext, version_id: str, request: PublishWorkflowRequest) -> WorkflowVersionView:
        """重新校验并原子发布 Workflow Version。

        仅 valid=true 时发布；固定所有 Role/Schema/Policy 精确版本与 content_hash；已发布内容
        不可修改。产生配置发布事件。
        """
        ...
    async def diff(self, actor: ActorContext, version_id: str, other_version_id: str) -> WorkflowDiff:
        """按稳定 node/edge key 比较两个可见版本，不调用模型做语义猜测。"""
        ...


def normalize_graph(graph: WorkflowGraph) -> WorkflowGraph:
    """Return the canonical representation used for graph/content hashes."""
    value = deepcopy(graph)
    value["nodes"] = sorted(value.get("nodes", []), key=lambda item: item["key"])
    value["edges"] = sorted(
        value.get("edges", []), key=lambda item: (item.get("priority", 0), item["key"])
    )
    for node in value["nodes"]:
        node["input_contracts"] = sorted(
            node.get("input_contracts", []), key=lambda item: json.dumps(item, sort_keys=True)
        )
        node["output_contracts"] = sorted(
            node.get("output_contracts", []), key=lambda item: json.dumps(item, sort_keys=True)
        )
    return value


def graph_hash(graph: WorkflowGraph) -> str:
    canonical = json.dumps(
        normalize_graph(graph), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WorkflowServiceImpl:
    """Concrete deterministic Workflow definition/version lifecycle service."""

    def __init__(
        self,
        repository: InMemoryWorkflowRepository | None = None,
        *,
        validator: WorkflowValidator | None = None,
        reference_resolver: object | None = None,
        event_publisher: object | None = None,
    ) -> None:
        self.repository = repository or InMemoryWorkflowRepository()
        self.validator = validator or StaticWorkflowValidator()
        self.reference_resolver = reference_resolver
        self.event_publisher = event_publisher
        self._idempotency: dict[tuple[str, str], tuple[str, object]] = {}

    @staticmethod
    def _actor_id(actor: ActorContext) -> str:
        actor_id = actor.get("user_id") if isinstance(actor, dict) else None
        if not actor_id:
            raise ArgumentError("actor user_id is required")
        return str(actor_id)

    def _idempotent(self, operation: str, key: str, payload: object) -> object | None:
        if not key:
            raise ArgumentError("idempotency_key is required")
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        current = self._idempotency.get((operation, key))
        if current is None:
            return None
        if current[0] != digest:
            raise IdempotencyConflictError("idempotency key was used with a different request")
        return deepcopy(current[1])

    def _remember(self, operation: str, key: str, payload: object, result: object) -> None:
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        self._idempotency[(operation, key)] = (digest, deepcopy(result))

    async def create_workflow(
        self, actor: ActorContext, request: CreateWorkflowRequest
    ) -> WorkflowView:
        self._actor_id(actor)
        cached = self._idempotent("create_workflow", request.get("idempotency_key", ""), request)
        if cached is not None:
            return cached  # type: ignore[return-value]
        key = request.get("key", "").strip()
        name = request.get("name", "").strip()
        if not key or not name:
            raise ArgumentError("workflow key and name are required")
        item: WorkflowView = {
            "id": str(uuid.uuid4()),
            "key": key,
            "name": name,
            "description": request.get("description", "").strip(),
            "latest_published_version_id": None,
            "version": 1,
        }
        saved = await self.repository.save_workflow(item)
        self._remember("create_workflow", request["idempotency_key"], request, saved)
        return saved

    async def create_version(
        self, actor: ActorContext, workflow_id: str, request: CreateWorkflowVersionRequest
    ) -> WorkflowVersionView:
        self._actor_id(actor)
        operation = f"create_version:{workflow_id}"
        cached = self._idempotent(operation, request.get("idempotency_key", ""), request)
        if cached is not None:
            return cached  # type: ignore[return-value]
        if await self.repository.get_workflow(workflow_id) is None:
            raise NotFoundError(f"workflow not found: {workflow_id}")
        base_id = request.get("based_on_version_id")
        base_graph: WorkflowGraph | None = None
        if base_id:
            base = await self.repository.get_version(base_id)
            if base is None or base["workflow_id"] != workflow_id:
                raise NotFoundError(f"base workflow version not found: {base_id}")
            base_graph = await self.repository.load_graph(base_id)
        item: WorkflowVersionView = {
            "id": str(uuid.uuid4()),
            "workflow_id": workflow_id,
            "version": await self.repository.next_version_number(workflow_id),
            "status": "DRAFT",
            "graph_hash": graph_hash(base_graph) if base_graph else None,
            "validation_status": "NOT_RUN",
            "content_hash": None,
            "revision": 1,
            "change_summary": request.get("change_summary", "").strip(),
        }
        saved = await self.repository.save_version(item)
        if base_graph is not None:
            await self.repository.replace_graph(saved["id"], base_graph)
        self._remember(operation, request["idempotency_key"], request, saved)
        return saved

    async def save_graph(
        self, actor: ActorContext, version_id: str, request: SaveGraphRequest
    ) -> WorkflowVersionView:
        self._actor_id(actor)
        operation = f"save_graph:{version_id}"
        cached = self._idempotent(operation, request.get("idempotency_key", ""), request)
        if cached is not None:
            return cached  # type: ignore[return-value]
        version = await self.repository.get_version(version_id)
        if version is None:
            raise NotFoundError(f"workflow version not found: {version_id}")
        if version["status"] != "DRAFT":
            raise VersionConflictError("only DRAFT workflow versions can be edited")
        expected = request.get("expected_version")
        if expected != int(version.get("revision", 1)):
            raise VersionConflictError("workflow graph revision conflict")
        normalized = normalize_graph(request["graph"])
        report = self.validator.validate(normalized)
        updated = deepcopy(version)
        updated["graph_hash"] = graph_hash(normalized)
        updated["validation_status"] = "PASS" if report["valid"] else "FAIL"
        atomic_save = getattr(self.repository, "replace_graph_with_version", None)
        if atomic_save is not None:
            saved = await atomic_save(version_id, normalized, updated, expected)
        else:
            await self.repository.replace_graph(version_id, normalized)
            saved = await self.repository.save_version(updated, expected_version=expected)
        self._remember(operation, request["idempotency_key"], request, saved)
        return saved

    async def validate_version(
        self, actor: ActorContext, version_id: str
    ) -> ValidationReport:
        self._actor_id(actor)
        version = await self.repository.get_version(version_id)
        graph = await self.repository.load_graph(version_id)
        if version is None or graph is None:
            raise NotFoundError(f"workflow graph not found: {version_id}")
        return self.validator.validate(graph)

    async def publish(
        self, actor: ActorContext, version_id: str, request: PublishWorkflowRequest
    ) -> WorkflowVersionView:
        actor_id = self._actor_id(actor)
        operation = f"publish:{version_id}"
        cached = self._idempotent(operation, request.get("idempotency_key", ""), request)
        if cached is not None:
            return cached  # type: ignore[return-value]
        version = await self.repository.get_version(version_id)
        graph = await self.repository.load_graph(version_id)
        if version is None or graph is None:
            raise NotFoundError(f"workflow graph not found: {version_id}")
        if version["status"] != "DRAFT":
            raise VersionConflictError("only DRAFT workflow versions can be published")
        expected = request.get("expected_version")
        if expected != int(version.get("revision", 1)):
            raise VersionConflictError("workflow publish revision conflict")
        if self.reference_resolver is not None:
            resolve = getattr(self.reference_resolver, "resolve_graph_references")
            resolved = resolve(graph)
            graph = await resolved if inspect.isawaitable(resolved) else resolved
            await self.repository.replace_graph(version_id, normalize_graph(graph))
        report = self.validator.validate(graph)
        if not report["valid"]:
            raise ValidationError("workflow validation failed", context={"errors": report["errors"]})
        updated = deepcopy(version)
        updated["status"] = "PUBLISHED"
        updated["validation_status"] = "PASS"
        updated["graph_hash"] = graph_hash(graph)
        content = {"version": {k: v for k, v in updated.items() if k != "content_hash"}, "graph": normalize_graph(graph)}
        updated["content_hash"] = hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        saved = await self.repository.save_version(updated, expected_version=expected)
        await self.repository.set_latest_published(saved["workflow_id"], saved["id"])
        if self.event_publisher is not None:
            publish = getattr(self.event_publisher, "publish", self.event_publisher)
            result = publish({"event_type": "workflow.published", "workflow_version_id": saved["id"], "content_hash": saved["content_hash"], "actor_id": actor_id})
            if inspect.isawaitable(result):
                await result
        self._remember(operation, request["idempotency_key"], request, saved)
        return saved

    async def diff(
        self, actor: ActorContext, version_id: str, other_version_id: str
    ) -> WorkflowDiff:
        self._actor_id(actor)
        base = await self.repository.load_graph(version_id)
        other = await self.repository.load_graph(other_version_id)
        if base is None or other is None:
            raise NotFoundError("one or both workflow graphs do not exist")
        base_nodes = {item["key"]: item for item in base["nodes"]}
        other_nodes = {item["key"]: item for item in other["nodes"]}
        base_edges = {item["key"]: item for item in base["edges"]}
        other_edges = {item["key"]: item for item in other["edges"]}
        changed_nodes = [
            {"key": key, "before": base_nodes[key], "after": other_nodes[key]}
            for key in sorted(base_nodes.keys() & other_nodes.keys())
            if base_nodes[key] != other_nodes[key]
        ]
        changed_edges = [
            {"key": key, "before": base_edges[key], "after": other_edges[key]}
            for key in sorted(base_edges.keys() & other_edges.keys())
            if base_edges[key] != other_edges[key]
        ]
        return {
            "base_version_id": version_id,
            "other_version_id": other_version_id,
            "added_nodes": sorted(other_nodes.keys() - base_nodes.keys()),
            "removed_nodes": sorted(base_nodes.keys() - other_nodes.keys()),
            "changed_nodes": changed_nodes,
            "added_edges": sorted(other_edges.keys() - base_edges.keys()),
            "removed_edges": sorted(base_edges.keys() - other_edges.keys()),
            "changed_edges": changed_edges,
        }
