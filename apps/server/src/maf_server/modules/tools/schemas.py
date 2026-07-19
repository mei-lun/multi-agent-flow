"""Tool、MCP 同步与策略模拟契约。

TASK-048 扩展：
- 保留原有 ``RegisterToolRequest`` / ``ToolView`` / ``SyncMcpToolsRequest`` 等
  TypedDict（其他任务接口契约，TASK-049/050 范围）；
- 新增 ``ToolRegistrationView`` / ``ToolVersionView`` / ``ToolListResult`` /
  ``UnregisterToolResult``，用于 Tool Registry 注册、查询、版本列表与注销接口。

字段命名遵循《多 Agent 协同工具系统设计文档》§11.4 ToolRegistry。
``version`` 为字符串语义版本，``version_no`` 为按 name 内部自增的整数序号，
用于乐观锁与顺序索引。
"""

from typing import Any, Literal, NotRequired, TypedDict


# --------------------------------------------------------------------------- #
# TASK-048 之前已有的契约（TASK-049/050 范围引用，保留不动）
# --------------------------------------------------------------------------- #


class RegisterToolRequest(TypedDict):
    key: str
    name: str
    adapter_type: Literal["NATIVE", "HTTP", "MCP"]
    endpoint_ref: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    approval_mode: Literal["NEVER", "POLICY", "ALWAYS"]
    timeout_seconds: int
    idempotency_key: str


class ToolView(TypedDict):
    id: str
    key: str
    version: int
    name: str
    adapter_type: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    risk_level: str
    approval_mode: str
    status: Literal["ACTIVE", "DISABLED"]


class SyncMcpToolsRequest(TypedDict):
    replace_missing: bool
    idempotency_key: str


class SyncMcpToolsResult(TypedDict):
    created_tool_ids: list[str]
    updated_tool_ids: list[str]
    disabled_tool_ids: list[str]
    warnings: list[str]


class PolicySimulationRequest(TypedDict):
    subject: dict[str, Any]
    action: str
    resource: str
    context: dict[str, Any]


class CapabilityDecisionView(TypedDict):
    allowed: bool
    decision_id: str
    policy_version_id: str
    reason_code: str
    requires_approval: bool
    approval_type: str | None
    constrained_arguments: dict[str, Any] | None
    obligations: list[dict[str, Any]]


# --------------------------------------------------------------------------- #
# TASK-048 Tool Registry 契约
# --------------------------------------------------------------------------- #


class ToolRegistrationView(TypedDict):
    """Tool Registry 单条 Tool 注册视图。

    - ``id``：注册记录主键（UUID）；
    - ``name`` / ``version``：业务唯一键，``UNIQUE(name, version)``；
    - ``description`` / ``input_schema`` / ``output_schema`` / ``capabilities`` /
      ``adapter_type``：从 ``ToolAdapter.metadata`` 读取并持久化的元数据；
    - ``version_no``：按 ``name`` 内部自增的整数序号，第一个版本为 1，
      新版本注册时 ``MAX(version_no) + 1``；
    - ``created_at``：RFC 3339 时间戳；
    - ``created_by``：注册者 ``user_id``。
    """

    id: str
    name: str
    version: str
    description: str
    adapter_type: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    capabilities: list[str]
    version_no: int
    created_at: str
    created_by: str


class ToolVersionView(TypedDict):
    """Tool 版本列表条目，``list_versions`` 返回该列表。"""

    version: str
    version_no: int
    description: str
    adapter_type: str
    created_at: str
    created_by: str


class ToolListResult(TypedDict):
    """``list_tools`` 返回的列表结果。"""

    items: list[ToolRegistrationView]


class UnregisterToolResult(TypedDict):
    """``unregister_tool`` 返回的删除结果。"""

    name: str
    version: str
    deleted: bool


# --------------------------------------------------------------------------- #
# TASK-049 MCP 工具发现契约
# --------------------------------------------------------------------------- #


class SyncError(TypedDict):
    """单条工具同步错误；同步过程对单个工具的失败不中断整体同步。

    - ``tool_name``：出错的远端工具名；连接级错误填 ``"<server>"``；
    - ``code``：错误码短串（如 ``"ALREADY_EXISTS"`` / ``"CONNECT_FAILED"`` /
      ``"INVALID_SCHEMA"``）；
    - ``message``：人类可读错误描述，**不得包含凭据明文**。
    """

    tool_name: str
    code: str
    message: str


class SyncResult(TypedDict):
    """``McpToolSyncService.sync_mcp_tools`` 返回的同步结果。

    - ``synced``：本次新注册的 Tool 视图列表（``ToolRegistrationView``，
      即 ``ToolRegistryService.register_tool`` 的返回值）；
    - ``skipped``：本次跳过的工具名列表（幂等重同步命中已注册即跳过）；
    - ``errors``：单工具级错误列表（连接错误也归入此处）；
    - ``server_url``：被同步的 MCP 服务器 url；
    - ``synced_count`` / ``skipped_count`` / ``error_count``：便捷计数。

    说明：任务文档将 ``synced`` 标注为 ``list[ToolView]``，但 ``ToolView`` 是
    TASK-048 之前的旧契约（含 ``key``/``risk_level``/``approval_mode``/``status``
    等字段，与 Registry 数据模型不符）。本任务改用 ``ToolRegistrationView``
    （``register_tool`` 实际返回类型），避免伪造字段。
    """

    server_url: str
    synced: list[ToolRegistrationView]
    skipped: list[str]
    errors: list[SyncError]
    synced_count: int
    skipped_count: int
    error_count: int


class McpServerView(TypedDict):
    """已配置的 MCP 服务器视图（``list_mcp_servers`` 返回条目）。

    - ``url``：MCP 服务器 endpoint（``mcp_servers`` 主键）；
    - ``name``：服务器展示名（从 url host 推导或调用方提供）；
    - ``credential_secret_id``：凭据 SecretService 引用 ID（无明文）；
    - ``last_synced_at``：上次同步时间戳，未同步为 ``None``；
    - ``synced_by``：上次同步执行者 user_id；
    - ``version_no``：服务器配置版本号，每次 upsert 自增。
    """

    url: str
    name: str
    credential_secret_id: str | None
    last_synced_at: str | None
    synced_by: str
    version_no: int


__all__ = [
    # 原有契约（保留）
    "CapabilityDecisionView",
    "PolicySimulationRequest",
    "RegisterToolRequest",
    "SyncMcpToolsRequest",
    "SyncMcpToolsResult",
    "ToolView",
    # TASK-048 新增
    "ToolListResult",
    "ToolRegistrationView",
    "ToolVersionView",
    "UnregisterToolResult",
    # TASK-049 新增
    "McpServerView",
    "SyncError",
    "SyncResult",
]
