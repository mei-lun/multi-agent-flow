"""Tool 注册与策略配置接口；实际调用位于 Tool Gateway。

TASK-048 扩展：
- 保留原有 ``ToolConfigurationService`` Protocol（其他任务接口契约，
  TASK-049/050 范围）；
- 新增 ``ToolRegistryService`` 具体实现，提供 Tool Registry 的注册、查询、
  版本列表与注销能力；
- 注册流程：校验 Adapter.metadata → 检查 DESIGNER/ADMIN 权限 → 检查
  name+version 唯一 → 计算 ``version_no`` → INSERT → 发布 ``ToolRegistered``
  事件 → commit；
- **注册过程绝不调用 ``ToolAdapter.invoke``**（TASK-048 验收）。

事件契约：
- ``ToolRegistered`` 事件 ``event_type`` = ``"tool.registered"``，
  ``aggregate_type`` = ``"Tool"``，``aggregate_id`` = ``"{name}:{version}"``，
  payload 含 ``name`` / ``version`` / ``version_no`` / ``adapter_type`` /
  ``capabilities`` / ``created_by``。

权限模型（与 ``maf_policy.DEFAULT_POLICIES`` 对齐）：
- ``register_tool`` / ``unregister_tool`` 是写操作 → ``action="write"``，
  ``resource="tools"``；
- ``DESIGNER`` 与 ``ADMIN`` 有 ``tools:write``；``OBSERVER`` 只读；
- 注销（``unregister_tool``）在基线策略下同样要求 ``tools:write``；
  任务说明要求 "仅 ADMIN 可注销"，故 service 在权限通过后再加 ADMIN-only 检查。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlparse

from maf_contracts.common import ActorContext
from maf_contracts.events import ActorRef, DomainEvent
from maf_domain.errors import (
    AlreadyExistsError,
    ArgumentError,
    NotFoundError,
    PermissionDeniedError,
)
from maf_policy import CasbinPermissionService
from maf_tool_adapters import EchoToolAdapter, ToolAdapter, ToolMetadata
from maf_server.core.database import Database
from maf_server.core.events import SqliteEventPublisher, init_outbox_schema
from maf_server.core.unit_of_work import SqliteUnitOfWork
from maf_server.gateway.secrets.service import SecretService
from maf_server.gateway.tool.mcp import McpClient, McpClientLike, McpToolInfo

from .repository import (
    McpServerRecord,
    SqliteMcpServerRepository,
    SqliteToolRegistryRepository,
    ToolRecord,
    init_mcp_servers_schema,
    init_tool_registry_schema,
    mcp_server_to_view,
    new_tool_id,
    record_to_view,
    record_to_version_view,
)
from .schemas import (
    CapabilityDecisionView,
    McpServerView,
    PolicySimulationRequest,
    RegisterToolRequest,
    SyncError,
    SyncMcpToolsRequest,
    SyncMcpToolsResult,
    SyncResult,
    ToolListResult,
    ToolRegistrationView,
    ToolVersionView,
    ToolView,
    UnregisterToolResult,
)

# --------------------------------------------------------------------------- #
# 资源/动作常量（与 maf_policy.DEFAULT_POLICIES 对齐）
# --------------------------------------------------------------------------- #

RESOURCE_TOOLS: str = "tools"
ACTION_READ: str = "read"
ACTION_WRITE: str = "write"

#: ToolRegistered 事件类型稳定字符串。
TOOL_REGISTERED_EVENT_TYPE: str = "tool.registered"

#: ToolRegistered 事件 schema 版本。
TOOL_REGISTERED_SCHEMA_VERSION: int = 1

_JSON_SCHEMA_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


def _validate_json_schema(schema: dict[str, Any], field: str) -> None:
    """Validate the structural subset accepted by the local Tool Gateway.

    This rejects malformed schemas during registration; runtime argument
    validation uses the exact persisted schema and never treats an invalid
    definition as permissive.
    """
    # The empty object is the standard permissive JSON Schema and is used by
    # metadata-only registrations that have no narrower argument contract.
    if not schema:
        return
    schema_type = schema.get("type")
    if schema_type not in _JSON_SCHEMA_TYPES:
        raise ArgumentError(f"ToolMetadata.{field}.type is invalid")
    properties = schema.get("properties")
    if properties is not None:
        if schema_type != "object" or not isinstance(properties, dict):
            raise ArgumentError(f"ToolMetadata.{field}.properties is invalid")
        for key, subschema in properties.items():
            if not isinstance(key, str) or not isinstance(subschema, dict):
                raise ArgumentError(f"ToolMetadata.{field}.properties is invalid")
            _validate_json_schema(subschema, f"{field}.properties.{key}")
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list)
        or not all(isinstance(item, str) for item in required)
        or properties is None
        or not set(required).issubset(properties)
    ):
        raise ArgumentError(f"ToolMetadata.{field}.required is invalid")
    items = schema.get("items")
    if items is not None:
        if schema_type != "array" or not isinstance(items, dict):
            raise ArgumentError(f"ToolMetadata.{field}.items is invalid")
        _validate_json_schema(items, f"{field}.items")


# --------------------------------------------------------------------------- #
# 原有 Protocol（保留 TASK-049/050 接口契约）
# --------------------------------------------------------------------------- #


class ToolConfigurationService(Protocol):
    async def register_tool(self, actor: ActorContext, request: RegisterToolRequest) -> ToolView:
        """注册工具元数据而不执行工具。

        验证稳定 key、输入输出 JSON Schema、adapter 引用、风险等级、超时和审批模式；
        NATIVE key 必须已在白名单注册，HTTP URL 必须符合网络策略，MCP 必须来自已配置
        Server。保存版本并写审计事件。
        """
        ...

    async def sync_mcp_tools(
        self,
        server_url: str,
        *,
        credential_secret_id: str | None = None,
        actor: ActorContext,
    ) -> SyncResult:
        """从 MCP Server 发现工具并幂等注册到 Tool Registry。

        连接 MCP 服务器、``list_tools`` 获取远端工具、逐个转换为 ``ToolMetadata``
        （``adapter_type="MCP"``）并调用 ``ToolRegistryService.register_tool``；
        重复同步命中已注册的 ``name+version`` 时跳过并记入 ``skipped``，不报错。
        不得在同步时执行工具（不调用 ``tools/call``）。
        """
        ...

    async def simulate_policy(
        self, actor: ActorContext, request: PolicySimulationRequest
    ) -> CapabilityDecisionView:
        """用真实策略引擎进行无副作用模拟。

        返回命中策略、约束后参数和审批要求，但不执行 Tool/Model。只有策略管理员可提交
        任意 subject，普通用户只能模拟自己。
        """
        ...


# --------------------------------------------------------------------------- #
# 权限服务 Protocol（避免直接依赖 IAM 模块）
# --------------------------------------------------------------------------- #


class PermissionService(Protocol):
    """权限服务协议，与 ``maf_policy.CasbinPermissionService`` 对齐。"""

    async def require(self, actor: ActorContext, action: str, resource: str) -> None:
        """无返回表示允许；无匹配授权、上下文缺失或策略异常都必须拒绝。"""
        ...


# --------------------------------------------------------------------------- #
# ToolRegistryService 具体实现
# --------------------------------------------------------------------------- #


class ToolRegistryService:
    """Tool Registry 应用服务：注册、查询、版本列表、注销。

    依赖注入：
        - ``database``：``Database``，提供 ``SqliteUnitOfWork`` 事务边界；
        - ``repository``：``SqliteToolRegistryRepository``，``tools`` 表 CRUD；
        - ``permission_service``：``PermissionService``，默认 ``CasbinPermissionService``；
        - ``clock``：可选 ``Clock``-like 对象，提供 ``now()`` 返回 ``datetime``。

    事务边界：
        - 写用例（``register_tool`` / ``unregister_tool``）在 ``SqliteUnitOfWork``
          事务内执行：写 ``tools`` 表 + 写 ``outbox_events``，原子提交/回滚；
        - 读用例（``list_tools`` / ``get_tool`` / ``list_versions``）使用
          ``database.read_connection``，无事务。

    安全约束：
        - **注册过程绝不调用 ``ToolAdapter.invoke`` 或 ``cancel``**（TASK-048
          验收"注册过程不执行 Tool"）。Service 只读取 ``adapter.metadata``；
        - ``register_tool`` 要求 ``DESIGNER`` 或 ``ADMIN``（``tools:write``）；
        - ``unregister_tool`` 在 ``tools:write`` 基础上额外要求 ``ADMIN`` 角色
          （任务说明："仅 ADMIN"）；
        - ``ActorContext`` 必须含非空 ``user_id`` 与 ``permission_keys``，
          ``permission_service.require`` 在异常路径下默认拒绝。
    """

    def __init__(
        self,
        database: Database,
        repository: SqliteToolRegistryRepository | None = None,
        permission_service: PermissionService | None = None,
        clock: "ClockLike | None" = None,
        native_allowlist: tuple[type, ...] | None = None,
    ) -> None:
        self._database: Database = database
        self._repository: SqliteToolRegistryRepository = (
            repository or SqliteToolRegistryRepository()
        )
        self._permission_service: PermissionService = (
            permission_service or CasbinPermissionService()
        )
        self._clock: ClockLike = clock or _SystemClock()
        self._native_allowlist = native_allowlist or (EchoToolAdapter,)

    # ------------------------------------------------------------------ #
    # 注册
    # ------------------------------------------------------------------ #

    async def register_tool(
        self,
        adapter: ToolAdapter,
        *,
        actor: ActorContext,
    ) -> ToolRegistrationView:
        """注册新 Tool 或新版本；仅 DESIGNER/ADMIN 可调用。

        实现顺序（对应《接口设计与实现规范》§6）：
        1. 校验 ``actor`` 与 ``adapter.metadata``；
        2. ``permission_service.require(actor, "write", "tools")``：
           仅 DESIGNER/ADMIN 通过；
        3. 进入 ``SqliteUnitOfWork`` 写事务：
           a. 检查 ``name + version`` 是否已存在 → 已存在抛 ``AlreadyExistsError``；
           b. 计算 ``version_no = MAX(version_no) + 1``（按 name）；
           c. INSERT ``tools`` 行；
           d. 通过 ``SqliteEventPublisher`` 写 ``ToolRegistered`` 事件到
              ``outbox_events``；
           e. ``commit``；
        4. 返回 ``ToolRegistrationView``。

        安全约束：
            - 注册过程 **绝不调用** ``adapter.invoke`` / ``adapter.cancel``；
            - ``adapter.metadata`` 一次性读取后即与 adapter 解耦，后续不再持有
              adapter 引用。

        :param adapter: 实现 ``ToolAdapter`` Protocol 的 Adapter 实例。
        :param actor: 调用者上下文，必须含 ``user_id`` 与 ``permission_keys``。
        :returns: ``ToolRegistrationView``，含分配的 ``id`` 与 ``version_no``。
        :raises PermissionDeniedError: 非 DESIGNER/ADMIN 角色。
        :raises AlreadyExistsError: ``name + version`` 已注册。
        :raises ArgumentError: ``adapter.metadata`` 字段非法。
        """
        # 1. actor 基础校验（permission_service.require 会再做一次）
        self._require_actor(actor)

        # 2. 读取 metadata（不调用 invoke / cancel）
        metadata: ToolMetadata = self._read_metadata(adapter)
        if metadata.adapter_type == "NATIVE" and not isinstance(adapter, self._native_allowlist) and not getattr(adapter, "is_registration_descriptor", False):
            raise ArgumentError("NATIVE Tool implementation is not in the startup allowlist")

        # 3. 权限检查：tools:write（DESIGNER/ADMIN 通过）
        await self._permission_service.require(actor, ACTION_WRITE, RESOURCE_TOOLS)

        # 4. 进入事务
        async with SqliteUnitOfWork(self._database) as uow:
            conn = uow.connection

            # 4a. 唯一性检查
            existing = await self._repository.get_by_name_version(
                conn, metadata.name, metadata.version
            )
            if existing is not None:
                # 抛出后 __aexit__ 自动 ROLLBACK
                raise AlreadyExistsError(
                    f"Tool 已注册: name={metadata.name!r}, version={metadata.version!r}",
                    context={
                        "name": metadata.name,
                        "version": metadata.version,
                    },
                )

            # 4b. 计算 version_no
            version_no = await self._repository.next_version_no(conn, metadata.name)

            # 4c. 构造记录并 INSERT
            now = self._clock.now()
            created_at = _ensure_iso(now)
            record = ToolRecord(
                id=new_tool_id(),
                name=metadata.name,
                version=metadata.version,
                description=metadata.description,
                adapter_type=metadata.adapter_type,
                input_schema=dict(metadata.input_schema),
                output_schema=dict(metadata.output_schema),
                capabilities=list(metadata.capabilities),
                created_at=created_at,
                created_by=actor["user_id"],
                version_no=version_no,
            )
            try:
                await self._repository.insert(conn, record)
            except sqlite3.IntegrityError as exc:
                # 并发场景下 UNIQUE(name, version) 冲突
                raise AlreadyExistsError(
                    f"Tool 已注册（并发冲突）: name={metadata.name!r}, "
                    f"version={metadata.version!r}",
                    context={
                        "name": metadata.name,
                        "version": metadata.version,
                    },
                ) from exc

            # 4d. 发布 ToolRegistered 事件到 outbox
            publisher = SqliteEventPublisher(conn)
            await publisher.append(self._build_tool_registered_event(record, actor))

            # 4e. 提交
            await uow.commit()

        return record_to_view(record)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    async def list_tools(self) -> ToolListResult:
        """列出全部已注册 Tool，按 name、version_no 升序。

        读用例，使用 ``database.read_connection``，无事务、无权限检查
        （列出本身不暴露敏感字段；具体资源访问由 CapabilityPolicy 控制，
        TASK-050 范围）。
        """
        async with self._database.read_connection() as conn:
            records = await self._repository.list_all(conn)
        return ToolListResult(items=[record_to_view(r) for r in records])

    async def get_tool(self, name: str, version: str) -> ToolRegistrationView:
        """按 ``name + version`` 获取；不存在抛 ``NotFoundError``。

        读用例，无权限检查。
        """
        async with self._database.read_connection() as conn:
            record = await self._repository.get_by_name_version(conn, name, version)
        if record is None:
            raise NotFoundError(
                f"Tool 不存在: name={name!r}, version={version!r}",
                context={"name": name, "version": version},
            )
        return record_to_view(record)

    async def list_versions(self, name: str) -> list[ToolVersionView]:
        """列出指定 ``name`` 的全部版本，按 ``version_no`` 升序。

        读用例，无权限检查。无任何版本时返回空列表。
        """
        async with self._database.read_connection() as conn:
            records = await self._repository.list_versions_by_name(conn, name)
        return [record_to_version_view(r) for r in records]

    # ------------------------------------------------------------------ #
    # 注销
    # ------------------------------------------------------------------ #

    async def unregister_tool(
        self,
        name: str,
        version: str,
        *,
        actor: ActorContext,
    ) -> UnregisterToolResult:
        """注销指定版本；仅 ADMIN 可调用。

        实现顺序：
        1. 校验 ``actor``；
        2. ``permission_service.require(actor, "write", "tools")``：
           DESIGNER/ADMIN 通过；
        3. 额外要求 ``actor.permission_keys`` 含 ``"ADMIN"``：
           任务说明"仅 ADMIN 可注销"；
        4. 进入 ``SqliteUnitOfWork`` 写事务：
           a. ``DELETE FROM tools WHERE name=? AND version=?``；
           b. 影响行数 0 → 抛 ``NotFoundError``；
           c. ``commit``；
        5. 返回 ``UnregisterToolResult``。

        保留同名其他版本；仅删除指定版本。
        """
        # 1. actor 校验
        self._require_actor(actor)

        # 2. tools:write 权限
        await self._permission_service.require(actor, ACTION_WRITE, RESOURCE_TOOLS)

        # 3. 额外要求 ADMIN
        if "ADMIN" not in (actor.get("permission_keys") or []):
            raise PermissionDeniedError(
                "注销 Tool 需要 ADMIN 角色",
                context={"action": "unregister", "resource": "tools"},
            )

        # 4. 进入事务
        async with SqliteUnitOfWork(self._database) as uow:
            conn = uow.connection
            deleted = await self._repository.delete_by_name_version(
                conn, name, version
            )
            if not deleted:
                raise NotFoundError(
                    f"Tool 不存在: name={name!r}, version={version!r}",
                    context={"name": name, "version": version},
                )
            await uow.commit()

        return UnregisterToolResult(name=name, version=version, deleted=True)

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _require_actor(actor: ActorContext) -> None:
        """校验 actor 必须含非空 user_id 与 permission_keys。"""
        if not isinstance(actor, dict):
            raise PermissionDeniedError("权限不足：调用者上下文缺失")
        user_id = actor.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise PermissionDeniedError("权限不足：未认证")
        permission_keys = actor.get("permission_keys")
        if not isinstance(permission_keys, list) or not permission_keys:
            raise PermissionDeniedError("权限不足：无授权角色")

    @staticmethod
    def _read_metadata(adapter: ToolAdapter) -> ToolMetadata:
        """从 adapter 读取并校验 ``ToolMetadata``。

        - 不调用 ``invoke`` / ``cancel``；
        - 校验 ``name`` / ``version`` 非空字符串；
        - 校验 ``adapter_type`` 取值合法。
        """
        if adapter is None:
            raise ArgumentError("adapter 不能为空")

        metadata_attr = getattr(adapter, "metadata", None)
        if metadata_attr is None:
            raise ArgumentError(
                "adapter.metadata 缺失：ToolAdapter 必须返回 ToolMetadata"
            )

        # ToolMetadata 是 dataclass，按属性访问
        name = getattr(metadata_attr, "name", None)
        version = getattr(metadata_attr, "version", None)
        if not isinstance(name, str) or not name:
            raise ArgumentError("ToolMetadata.name 必须是非空字符串")
        if not isinstance(version, str) or not version:
            raise ArgumentError("ToolMetadata.version 必须是非空字符串")

        adapter_type = getattr(metadata_attr, "adapter_type", "NATIVE")
        if adapter_type not in ("NATIVE", "HTTP", "MCP"):
            raise ArgumentError(
                f"ToolMetadata.adapter_type 非法: {adapter_type!r}，"
                f"应为 NATIVE/HTTP/MCP"
            )

        # 校验 input/output schema 可 JSON 序列化（与持久化层 json_valid 对齐）
        input_schema = getattr(metadata_attr, "input_schema", {}) or {}
        output_schema = getattr(metadata_attr, "output_schema", {}) or {}
        capabilities = getattr(metadata_attr, "capabilities", []) or []
        if not isinstance(input_schema, dict):
            raise ArgumentError("ToolMetadata.input_schema 必须是 dict")
        if not isinstance(output_schema, dict):
            raise ArgumentError("ToolMetadata.output_schema 必须是 dict")
        _validate_json_schema(input_schema, "input_schema")
        _validate_json_schema(output_schema, "output_schema")
        if not isinstance(capabilities, list):
            raise ArgumentError("ToolMetadata.capabilities 必须是 list")
        for cap in capabilities:
            if not isinstance(cap, str) or not cap:
                raise ArgumentError(
                    "ToolMetadata.capabilities 每项必须是非空字符串"
                )

        description = getattr(metadata_attr, "description", "") or ""

        # 返回规范化后的 ToolMetadata 副本，避免后续被外部修改
        return ToolMetadata(
            name=name,
            version=version,
            description=str(description),
            input_schema=dict(input_schema),
            output_schema=dict(output_schema),
            capabilities=[str(c) for c in capabilities],
            adapter_type=str(adapter_type),
        )

    @staticmethod
    def _build_tool_registered_event(
        record: ToolRecord,
        actor: ActorContext,
    ) -> DomainEvent:
        """构造 ``ToolRegistered`` 领域事件。

        ``event_type`` = ``"tool.registered"``，``aggregate_type`` = ``"Tool"``，
        ``aggregate_id`` = ``"{name}:{version}"``。
        payload 含 ``name`` / ``version`` / ``version_no`` / ``adapter_type`` /
        ``capabilities`` / ``created_by``。
        """
        return DomainEvent(
            event_type=TOOL_REGISTERED_EVENT_TYPE,
            schema_version=TOOL_REGISTERED_SCHEMA_VERSION,
            aggregate_type="Tool",
            aggregate_id=f"{record.name}:{record.version}",
            organization_id=actor.get("organization_id", ""),
            project_id=None,
            run_id=None,
            occurred_at=datetime.now(timezone.utc),
            actor=ActorRef(
                actor_type="USER",
                actor_id=actor.get("user_id", ""),
            ),
            trace_id=actor.get("trace_id", ""),
            payload={
                "name": record.name,
                "version": record.version,
                "version_no": record.version_no,
                "adapter_type": record.adapter_type,
                "capabilities": list(record.capabilities),
                "created_by": record.created_by,
                "description": record.description,
            },
        )


# --------------------------------------------------------------------------- #
# 内部时钟（避免直接依赖 maf_server.core.clock，保持本模块独立可测试）
# --------------------------------------------------------------------------- #


class ClockLike(Protocol):
    """最小时钟协议，供 ``ToolRegistryService`` 注入虚拟时钟。"""

    def now(self) -> datetime: ...


class _SystemClock:
    """默认系统 UTC 时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def _ensure_iso(value: datetime) -> str:
    """把 datetime 序列化为带时区 ISO 8601 字符串；naive 视为 UTC。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


# --------------------------------------------------------------------------- #
# Schema 初始化辅助（供测试与首次启动使用）
# --------------------------------------------------------------------------- #


async def init_tool_registry_schema_on_database(database: Database) -> None:
    """在 ``Database`` 上创建 ``tools`` / ``outbox_events`` / ``mcp_servers`` 表。

    供测试与首次启动使用；正式部署由 ``migrations/`` 顺序迁移负责。
    本函数幂等，可重复调用。

    TASK-049 扩展：同时创建 ``mcp_servers`` 表，供 ``McpToolSyncService`` 使用。
    """
    # 先建 outbox_events（事件依赖）
    await init_outbox_schema(database)
    # 再建 tools 与 mcp_servers 表
    async with database.write_connection() as conn:
        await init_tool_registry_schema(conn)
        await init_mcp_servers_schema(conn)


# --------------------------------------------------------------------------- #
# TASK-049: McpToolSyncService
# --------------------------------------------------------------------------- #


#: MCP 工具注册时使用的默认版本字符串。
#: 远端工具未声明版本时统一使用 ``"1.0.0"``；重同步命中同名同版本即跳过（幂等）。
MCP_DEFAULT_TOOL_VERSION: str = "1.0.0"

#: MCP 工具注册时的默认能力标识。
MCP_DEFAULT_CAPABILITIES: list[str] = ["mcp"]

#: 凭据 resolve 用途；与 ``LocalSecretService`` 允许的 purpose 白名单对齐。
MCP_SECRET_PURPOSE: str = "mcp.call"


def _server_name_from_url(url: str) -> str:
    """从 url 推导 MCP 服务器展示名（host）；解析失败回退为原 url。"""
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if host:
            return host
    except Exception:  # noqa: BLE001 —— url 解析失败回退原值
        pass
    return url


def _safe_error_message(exc: BaseException) -> str:
    """提取异常消息并确保不含凭据明文（按异常类型白名单过滤）。

    MCP 客户端与 Registry 抛出的业务异常消息不含凭据；这里只取 ``str(exc)``
    并截断长度，避免把意外的大对象写入错误列表。凭据明文仅存在于
    ``SecretService.resolve`` 返回值，从不进入异常消息。
    """
    text = str(exc) or type(exc).__name__
    if len(text) > 256:
        text = text[:256] + "..."
    return text


class _McpToolAdapter:
    """把 ``McpToolInfo`` 包装成 ``ToolAdapter`` 鸭子类型。

    注册流程只读取 ``adapter.metadata``，不调用 ``invoke`` / ``cancel``；
    本类提供这两个方法的桩实现以满足 Protocol 结构，但不会被注册流程触发
    （与 ``router._StaticMetadataAdapter`` 一致）。

    - ``adapter_type = "MCP"``；
    - ``version`` 默认 ``MCP_DEFAULT_TOOL_VERSION``；
    - ``input_schema`` / ``output_schema`` 缺失时回退为 ``{"type": "object"}``
      占位，以满足 ``tools`` 表 ``json_valid`` 约束；
    - ``capabilities`` 默认 ``["mcp"]``，供 CapabilityPolicy 后续判定。
    """

    adapter_type: str = "MCP"

    def __init__(
        self,
        info: McpToolInfo,
        *,
        version: str = MCP_DEFAULT_TOOL_VERSION,
    ) -> None:
        self._metadata: ToolMetadata = ToolMetadata(
            name=info.name,
            version=version,
            description=info.description or "",
            input_schema=dict(info.input_schema) if info.input_schema else {"type": "object"},
            output_schema=dict(info.output_schema) if info.output_schema else {"type": "object"},
            capabilities=list(MCP_DEFAULT_CAPABILITIES),
            adapter_type="MCP",
        )

    @property
    def metadata(self) -> ToolMetadata:
        return self._metadata

    async def invoke(
        self,
        definition: dict[str, Any],
        arguments: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """注册流程不会调用本方法；仅满足 Protocol 结构。"""
        raise NotImplementedError("MCP 工具同步流程不调用 invoke")

    async def cancel(self, external_call_id: str) -> None:
        """注册流程不会调用本方法；仅满足 Protocol 结构。"""
        raise NotImplementedError("MCP 工具同步流程不调用 cancel")


class McpToolSyncService:
    """MCP 工具发现与同步应用服务。

    依赖注入：
        - ``database``：``Database``，提供 ``SqliteUnitOfWork`` 事务边界；
        - ``registry_service``：``ToolRegistryService``，复用其 ``register_tool``
          把 MCP 工具写入 ``tools`` 表（``adapter_type="MCP"``）；
        - ``secret_service``：``SecretService``，解析 MCP 服务器凭据（不持久化明文）；
        - ``mcp_client``：``McpClientLike``，MCP 客户端（默认 Mock ``McpClient``）；
        - ``mcp_server_repository``：``SqliteMcpServerRepository``，``mcp_servers`` CRUD；
        - ``permission_service``：``PermissionService``，默认 ``CasbinPermissionService``；
        - ``clock``：可选 ``ClockLike``。

    同步流程（``sync_mcp_tools``）：
        1. 校验 ``actor``；
        2. ``permission_service.require(actor, "write", "tools")``（ADMIN/DESIGNER）；
        3. 解析凭据（若 ``credential_secret_id`` 为空则回退 ``mcp_servers`` 已存引用）；
        4. ``mcp_client.connect`` → ``list_tools`` → ``disconnect``；
        5. 逐个工具转 ``ToolMetadata`` → ``registry_service.register_tool``；
           命中 ``AlreadyExistsError`` 记入 ``skipped``（幂等），不报错；
        6. upsert ``mcp_servers`` 配置（``last_synced_at`` / ``synced_by`` / 版本号）；
        7. 返回 ``SyncResult``。

    安全约束：
        - **不执行工具**：仅 ``list_tools`` 发现，绝不调用 ``tools/call``
          （TASK-049 验收"同步不执行远端 Tool"）；
        - **凭据不落明文**：``credential_secret_id`` 只存 SecretService 引用；
          明文经 ``resolve`` 取得后仅在内存会话期内传给 ``McpClient``，不写日志、
          不写数据库、不进异常消息；
        - **协议错误脱敏**：连接/列举错误归一化为 ``SyncError``，消息不含凭据；
        - **幂等**：重同步命中 ``name+version`` 已注册即跳过，不重复注册、不报错。

    权限模型：
        - ``sync_mcp_tools`` / ``remove_mcp_server``：``tools:write``（ADMIN/DESIGNER）；
        - ``list_mcp_servers``：``tools:read``（OBSERVER+）。
    """

    def __init__(
        self,
        database: Database,
        registry_service: ToolRegistryService,
        secret_service: SecretService,
        *,
        mcp_client: McpClientLike | None = None,
        mcp_server_repository: SqliteMcpServerRepository | None = None,
        permission_service: PermissionService | None = None,
        clock: "ClockLike | None" = None,
    ) -> None:
        self._database: Database = database
        self._registry_service: ToolRegistryService = registry_service
        self._secret_service: SecretService = secret_service
        self._mcp_client: McpClientLike = mcp_client or McpClient()
        self._mcp_server_repo: SqliteMcpServerRepository = (
            mcp_server_repository or SqliteMcpServerRepository()
        )
        self._permission_service: PermissionService = (
            permission_service or CasbinPermissionService()
        )
        self._clock: ClockLike = clock or _SystemClock()
        self._server_tool_names: dict[str, set[str]] = {}
        self._disabled_mcp_tools: dict[str, set[str]] = {}

    # ------------------------------------------------------------------ #
    # 同步
    # ------------------------------------------------------------------ #

    async def sync_mcp_tools(
        self,
        server_url: str,
        *,
        credential_secret_id: str | None = None,
        actor: ActorContext,
    ) -> SyncResult:
        """从 MCP 服务器发现工具并幂等注册到 Tool Registry。

        :param server_url: MCP 服务器 endpoint（非空）。
        :param credential_secret_id: 凭据 SecretService 引用 ID；为空时回退
            ``mcp_servers`` 已存引用。
        :param actor: 调用者上下文，必须含 ``user_id`` 与 ``permission_keys``。
        :returns: ``SyncResult``（``synced`` / ``skipped`` / ``errors``）。
        :raises PermissionDeniedError: 非 ADMIN/DESIGNER。
        :raises ArgumentError: ``server_url`` 为空。
        """
        # 1. actor 校验
        self._require_actor(actor)

        # 2. server_url 校验
        if not isinstance(server_url, str) or not server_url:
            raise ArgumentError(
                "MCP server_url 不能为空",
                context={"field": "server_url"},
            )

        # 3. 权限检查：tools:write（ADMIN/DESIGNER 通过）
        await self._permission_service.require(actor, ACTION_WRITE, RESOURCE_TOOLS)

        # 4. 解析凭据引用（传入优先，回退已存）
        effective_secret_id = await self._resolve_effective_secret_id(
            server_url, credential_secret_id
        )

        # 5. 解析凭据明文（仅内存，不写日志/DB）
        credentials: dict[str, Any] | None = None
        if effective_secret_id:
            try:
                plaintext = await self._secret_service.resolve(
                    effective_secret_id, MCP_SECRET_PURPOSE, actor["user_id"]
                )
            except Exception as exc:  # noqa: BLE001 —— 凭据解析失败归入错误
                return SyncResult(
                    server_url=server_url,
                    synced=[],
                    skipped=[],
                    errors=[
                        SyncError(
                            tool_name="<server>",
                            code="SECRET_RESOLVE_FAILED",
                            message=_safe_error_message(exc),
                        )
                    ],
                    synced_count=0,
                    skipped_count=0,
                    error_count=1,
                )
            credentials = self._credentials_from_plaintext(plaintext)

        # 6. 连接 + 列举 + 断开
        try:
            await self._mcp_client.connect(server_url, credentials=credentials)
            try:
                remote_tools = await self._mcp_client.list_tools()
            finally:
                await self._mcp_client.disconnect()
        except Exception as exc:  # noqa: BLE001 —— 连接/列举错误归一化
            return SyncResult(
                server_url=server_url,
                synced=[],
                skipped=[],
                errors=[
                    SyncError(
                        tool_name="<server>",
                        code="CONNECT_FAILED",
                        message=_safe_error_message(exc),
                    )
                ],
                synced_count=0,
                skipped_count=0,
                error_count=1,
            )

        # 7. 逐个工具注册（幂等：命中已注册即跳过）
        synced: list[ToolRegistrationView] = []
        skipped: list[str] = []
        errors: list[SyncError] = []

        for info in remote_tools:
            name = info.name
            if not isinstance(name, str) or not name:
                errors.append(
                    SyncError(
                        tool_name="<unknown>",
                        code="INVALID_SCHEMA",
                        message="MCP 工具 name 为空",
                    )
                )
                continue

            adapter = _McpToolAdapter(info)
            try:
                view = await self._registry_service.register_tool(adapter, actor=actor)
                synced.append(view)
            except AlreadyExistsError:
                # 幂等：同名同版本已注册，跳过不报错
                skipped.append(name)
            except ArgumentError as exc:
                errors.append(
                    SyncError(
                        tool_name=name,
                        code="INVALID_SCHEMA",
                        message=_safe_error_message(exc),
                    )
                )
            except Exception as exc:  # noqa: BLE001 —— 单工具失败不中断整体
                errors.append(
                    SyncError(
                        tool_name=name,
                        code="REGISTER_FAILED",
                        message=_safe_error_message(exc),
                    )
                )

        present_names = {
            info.name for info in remote_tools
            if isinstance(info.name, str) and info.name
        }
        previous_names = self._server_tool_names.get(server_url, set())
        missing_names = previous_names - present_names
        self._disabled_mcp_tools.setdefault(server_url, set()).update(missing_names)
        self._disabled_mcp_tools[server_url].difference_update(present_names)
        self._server_tool_names[server_url] = present_names

        # 8. upsert mcp_servers 配置
        await self._upsert_server_config(
            server_url,
            credential_secret_id=(
                credential_secret_id
                if credential_secret_id is not None
                else effective_secret_id
            ),
            actor_id=actor["user_id"],
        )

        # 9. 返回结果
        return SyncResult(
            server_url=server_url,
            synced=synced,
            skipped=skipped,
            errors=errors,
            synced_count=len(synced),
            skipped_count=len(skipped),
            error_count=len(errors),
        )

    def disabled_tools(self, server_url: str) -> list[str]:
        """Return remote tools disabled by discovery without deleting history."""
        return sorted(self._disabled_mcp_tools.get(server_url, set()))

    # ------------------------------------------------------------------ #
    # 列表 / 移除
    # ------------------------------------------------------------------ #

    async def list_mcp_servers(self, *, actor: ActorContext) -> list[McpServerView]:
        """列出全部已配置的 MCP 服务器。

        :param actor: 调用者上下文；要求 ``tools:read``。
        :returns: ``McpServerView`` 列表，按 url 升序。
        :raises PermissionDeniedError: 无 ``tools:read`` 权限。
        """
        self._require_actor(actor)
        await self._permission_service.require(actor, ACTION_READ, RESOURCE_TOOLS)
        async with self._database.read_connection() as conn:
            records = await self._mcp_server_repo.list_all(conn)
        return [mcp_server_to_view(r) for r in records]

    async def remove_mcp_server(
        self,
        server_url: str,
        *,
        actor: ActorContext,
    ) -> None:
        """移除 MCP 服务器配置。

        仅删除 ``mcp_servers`` 配置行；已注册的 MCP 工具（``tools`` 表）保留
        （不删除历史）。不级联删除凭据 SecretService 引用。

        :param server_url: MCP 服务器 endpoint。
        :param actor: 调用者上下文；要求 ``tools:write``。
        :raises PermissionDeniedError: 非 ADMIN/DESIGNER。
        :raises NotFoundError: 服务器配置不存在。
        """
        self._require_actor(actor)
        await self._permission_service.require(actor, ACTION_WRITE, RESOURCE_TOOLS)

        if not isinstance(server_url, str) or not server_url:
            raise ArgumentError(
                "MCP server_url 不能为空",
                context={"field": "server_url"},
            )

        async with SqliteUnitOfWork(self._database) as uow:
            conn = uow.connection
            deleted = await self._mcp_server_repo.delete_by_url(conn, server_url)
            if not deleted:
                raise NotFoundError(
                    f"MCP 服务器配置不存在: url={server_url!r}",
                    context={"server_url": server_url},
                )
            await uow.commit()

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _require_actor(actor: ActorContext) -> None:
        """校验 actor 必须含非空 user_id 与 permission_keys。"""
        if not isinstance(actor, dict):
            raise PermissionDeniedError("权限不足：调用者上下文缺失")
        user_id = actor.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise PermissionDeniedError("权限不足：未认证")
        permission_keys = actor.get("permission_keys")
        if not isinstance(permission_keys, list) or not permission_keys:
            raise PermissionDeniedError("权限不足：无授权角色")

    async def _resolve_effective_secret_id(
        self,
        server_url: str,
        credential_secret_id: str | None,
    ) -> str | None:
        """确定生效的凭据引用：传入优先，为空时回退 ``mcp_servers`` 已存引用。"""
        if credential_secret_id is not None:
            return credential_secret_id
        # 回退已存配置
        async with self._database.read_connection() as conn:
            existing = await self._mcp_server_repo.get_by_url(conn, server_url)
        return existing.credential_secret_id if existing is not None else None

    @staticmethod
    def _credentials_from_plaintext(plaintext: str) -> dict[str, Any]:
        """把凭据明文转换为 ``McpClient.connect`` 接受的 dict。

        若明文是合法 JSON 对象，直接用作 credentials（支持多字段凭据）；
        否则包装为 ``{"token": plaintext}``。明文不写日志、不持久化。
        """
        try:
            parsed = json.loads(plaintext)
            if isinstance(parsed, dict):
                return dict(parsed)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        return {"token": plaintext}

    async def _upsert_server_config(
        self,
        server_url: str,
        credential_secret_id: str | None,
        actor_id: str,
    ) -> None:
        """upsert ``mcp_servers`` 配置行，更新 ``last_synced_at`` 与版本号。"""
        async with SqliteUnitOfWork(self._database) as uow:
            conn = uow.connection
            existing = await self._mcp_server_repo.get_by_url(conn, server_url)
            version_no = (existing.version_no + 1) if existing is not None else 1
            record = McpServerRecord(
                url=server_url,
                name=_server_name_from_url(server_url),
                credential_secret_id=credential_secret_id,
                last_synced_at=_ensure_iso(self._clock.now()),
                synced_by=actor_id,
                version_no=version_no,
            )
            await self._mcp_server_repo.upsert(conn, record)
            await uow.commit()


__all__ = [
    "ACTION_READ",
    "ACTION_WRITE",
    "MCP_DEFAULT_CAPABILITIES",
    "MCP_DEFAULT_TOOL_VERSION",
    "MCP_SECRET_PURPOSE",
    "RESOURCE_TOOLS",
    "TOOL_REGISTERED_EVENT_TYPE",
    "TOOL_REGISTERED_SCHEMA_VERSION",
    "McpToolSyncService",
    "PermissionService",
    "ToolConfigurationService",
    "ToolRegistryService",
    "init_tool_registry_schema_on_database",
]
