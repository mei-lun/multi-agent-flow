"""把已发布 Workflow Version 编译为 LangGraph 图的接口。"""

from typing import Any
import hashlib
import json
import inspect

from maf_server.scheduler.state import RunState
from maf_server.scheduler.graph_nodes.route import choose_next_edge
from maf_server.scheduler.graph_nodes.dispatch import dispatch_node
from maf_server.scheduler.graph_nodes.evaluate import evaluate_node


def compile_workflow(workflow_version: object, checkpointer: object) -> Any:
    """返回可执行但尚未运行的 compiled graph。

    输入必须是通过静态校验且不可变的 Workflow Version；为每种 node kind 映射固定节点
    函数，为 edge 使用受限条件求值器，绑定传入 checkpointer。编译不得读取项目当前配置、
    调用模型或写数据库。相同 content hash 应产生等价图；未知节点类型立即失败。
    """
    graph = _get(workflow_version, "graph", None) or _get(workflow_version, "workflow_graph", None)
    if graph is None and isinstance(workflow_version, dict) and "nodes" in workflow_version:
        graph = workflow_version
    if not isinstance(graph, dict):
        raise ValueError("published workflow version must contain a graph")
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if isinstance(nodes, list):
        node_map = {str(n["key"]): n for n in nodes if isinstance(n, dict) and n.get("key")}
    elif isinstance(nodes, dict):
        node_map = {str(k): v for k, v in nodes.items()}
    else:
        raise ValueError("workflow graph nodes must be a list or mapping")
    if not node_map:
        raise ValueError("workflow graph has no nodes")
    supported_kinds = {"AGENT", "GATE", "HUMAN_GATE", "END_SUCCESS", "END_FAILURE"}
    unknown = sorted({str(node.get("kind")) for node in node_map.values() if str(node.get("kind")) not in supported_kinds})
    if unknown:
        raise ValueError(f"unknown workflow node kind(s): {', '.join(unknown)}")
    start = str(graph.get("start_node_key") or next(iter(node_map)))
    if start not in node_map:
        raise ValueError(f"unknown start node {start!r}")
    outgoing: dict[str, list[dict[str, Any]]] = {key: [] for key in node_map}
    for edge in edges or []:
        if not isinstance(edge, dict):
            continue
        source, target = edge.get("source_node_key"), edge.get("target_node_key")
        if source not in node_map or target not in node_map:
            raise ValueError("workflow edge references unknown node")
        outgoing[source].append(edge)
    workflow_hash = str(_get(workflow_version, "content_hash", None) or _get(workflow_version, "graph_hash", None) or hashlib.sha256(json.dumps(graph, sort_keys=True, default=str).encode()).hexdigest())
    dispatcher = _get(workflow_version, "dispatcher", None)
    quality_gate = _get(workflow_version, "quality_gate", None)

    async def invoke_node(state: object, key: str) -> dict[str, Any]:
        rs = RunState.from_value(state)
        rs.workflow_version_id = rs.workflow_version_id or workflow_hash
        node = node_map[key]
        rs.metadata.setdefault("workflow_hash", workflow_hash)
        # Keep the checkpoint small: only dispatch fields are copied into
        # state.  The immutable graph remains in this compiled closure.
        if "nodes" not in rs.metadata:
            rs.metadata["nodes"] = {
                node_key: {
                    field: node.get(field)
                    for field in (
                        "role_snapshot_id",
                        "role_version_id",
                        "repository_binding_id",
                        "required_capabilities",
                        "base_commit",
                    )
                    if node.get(field) is not None
                }
                for node_key, node in node_map.items()
            }
        kind = str(node.get("kind", "AGENT"))
        if kind == "AGENT" and dispatcher is not None:
            rs = await dispatch_node(rs, key, dispatcher)
        elif kind == "GATE" and quality_gate is not None:
            attempt_id = str(rs.metadata.get("attempt_ids", {}).get(key, "")) if isinstance(rs.metadata.get("attempt_ids"), dict) else ""
            if attempt_id:
                rs = await evaluate_node(rs, key, attempt_id, quality_gate)
        elif kind == "END_SUCCESS":
            rs.status, rs.waiting_for = "COMPLETED", None
        elif kind == "END_FAILURE":
            rs.status, rs.waiting_for = "FAILED", None
        else:
            rs.current_node_ids = [key]
            if kind not in {"HUMAN_GATE"}:
                rs.status = "RUNNING"
            else:
                rs.status, rs.waiting_for = "WAITING_HUMAN", key
        return rs.to_dict()

    # LangGraph is optional for local development; use it whenever installed.
    try:
        from typing import Any as _Any, TypedDict
        from langgraph.graph import END, StateGraph  # type: ignore

        class GraphState(TypedDict, total=False):
            run_id: str
            workflow_version_id: str
            status: str
            current_node_ids: list[str]
            artifact_ids: list[str]
            attempt: int
            task_ids: dict[str, str]
            gate_decision_ids: list[str]
            rework_counts: dict[str, int]
            waiting_for: str | None
            metadata: dict[str, object]

        builder = StateGraph(GraphState)
        for key in node_map:
            def invoke_node_sync(state: object, key: str = key) -> dict[str, Any]:
                # The compiled graph supports the synchronous ``invoke`` API,
                # while dispatchers use an async repository/service contract.
                # LangGraph executes synchronous nodes outside its async loop,
                # so each invocation can own this short-lived event loop.
                import asyncio

                return asyncio.run(invoke_node(state, key))

            builder.add_node(key, invoke_node_sync)
        builder.set_entry_point(start)
        for source, source_edges in outgoing.items():
            if not source_edges:
                if node_map[source].get("kind") not in {"END_SUCCESS", "END_FAILURE"}:
                    builder.add_edge(source, END)
                continue
            mapping = {str(edge["target_node_key"]): str(edge["target_node_key"]) for edge in source_edges}
            mapping["__wait__"] = END
            def route(state: dict[str, Any], source: str = source, source_edges: list[dict[str, Any]] = source_edges) -> str:
                rs = RunState.from_value(state)
                if rs.status in {"WAITING_GIT_TASK", "WAITING_HUMAN", "PAUSED"}:
                    return "__wait__"
                facts = dict(rs.metadata)
                facts.update({"status": rs.status, "attempt": rs.attempt})
                edge = choose_next_edge(rs, source_edges, facts)
                return str(edge["target_node_key"])
            builder.add_conditional_edges(source, route, mapping)
        return builder.compile(checkpointer=checkpointer)
    except ImportError:
        return _FallbackCompiledGraph(start, node_map, outgoing, invoke_node, checkpointer)


def _get(value: object, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


class _FallbackCompiledGraph:
    def __init__(self, start: str, nodes: dict[str, dict[str, Any]], outgoing: dict[str, list[dict[str, Any]]], invoke_node: Any, checkpointer: Any) -> None:
        self.start, self.nodes, self.outgoing, self._invoke_node, self.checkpointer = start, nodes, outgoing, invoke_node, checkpointer

    async def ainvoke(self, state: object | None, config: dict[str, Any] | None = None) -> dict[str, Any]:
        current = self.start
        config = config or {}
        if state is None and hasattr(self.checkpointer, "get"):
            state = self.checkpointer.get(config)
        if state is None:
            raise ValueError("initial RunState is required when no checkpoint exists")
        value: object = RunState.from_value(state).to_dict()
        if RunState.from_value(value).current_node_ids:
            current = RunState.from_value(value).current_node_ids[0]
        while True:
            value = await self._invoke_node(value, current)
            if hasattr(self.checkpointer, "put"):
                self.checkpointer.put(config, value)
            node = self.nodes[current]
            status = RunState.from_value(value).status
            if status in {"WAITING_GIT_TASK", "WAITING_HUMAN", "PAUSED"}:
                break
            if node.get("kind") in {"END_SUCCESS", "END_FAILURE"} or not self.outgoing.get(current):
                break
            edge = choose_next_edge(RunState.from_value(value), self.outgoing[current], RunState.from_value(value).metadata)
            current = str(edge["target_node_key"])
        return value  # type: ignore[return-value]

    def invoke(self, state: object, config: dict[str, Any] | None = None) -> Any:
        import asyncio
        return asyncio.run(self.ainvoke(state, config))
