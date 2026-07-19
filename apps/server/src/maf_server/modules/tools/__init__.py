"""Native/MCP tool definitions, risk levels, scopes, and grants.

TASK-048 扩展：
- ``schemas``：Tool Registry 契约 TypedDict（``ToolRegistrationView`` 等）。
- ``repository``：``tools`` 表 DDL 与 ``SqliteToolRegistryRepository``。
- ``service``：``ToolRegistryService`` 应用服务（注册/查询/版本/注销）。
- ``router``：``build_tools_router`` FastAPI 路由工厂。

TASK-049 扩展：
- ``repository``：``mcp_servers`` 表 DDL 与 ``SqliteMcpServerRepository``。
- ``service``：``McpToolSyncService`` 应用服务（同步/列表/移除）。
- ``router``：``build_mcp_sync_router`` FastAPI 路由工厂。
- ``schemas``：``SyncResult`` / ``SyncError`` / ``McpServerView``。
"""

from .repository import (
    MCP_SERVERS_SCHEMA_SQL,
    McpServerRecord,
    SqliteMcpServerRepository,
    SqliteToolRegistryRepository,
    TOOL_REGISTRY_SCHEMA_SQL,
    ToolRecord,
    ToolRepository,
    init_mcp_servers_schema,
    init_tool_registry_schema,
    mcp_server_to_view,
    new_tool_id,
    record_to_view,
    record_to_version_view,
)
from .router import (
    McpServerOut,
    RemoveMcpServerPayload,
    SyncErrorOut,
    SyncMcpToolsPayload,
    SyncMcpToolsResultOut,
    SyncedToolOut,
    build_mcp_sync_router,
    build_tools_router,
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
from .service import (
    MCP_DEFAULT_CAPABILITIES,
    MCP_DEFAULT_TOOL_VERSION,
    MCP_SECRET_PURPOSE,
    McpToolSyncService,
    PermissionService,
    ToolConfigurationService,
    ToolRegistryService,
    init_tool_registry_schema_on_database,
)

__all__ = [
    "MCP_DEFAULT_CAPABILITIES",
    "MCP_DEFAULT_TOOL_VERSION",
    "MCP_SECRET_PURPOSE",
    "MCP_SERVERS_SCHEMA_SQL",
    "McpServerOut",
    "McpServerRecord",
    "McpServerView",
    "McpToolSyncService",
    "PermissionService",
    "PolicySimulationRequest",
    "RegisterToolRequest",
    "RemoveMcpServerPayload",
    "SqliteMcpServerRepository",
    "SqliteToolRegistryRepository",
    "SyncError",
    "SyncErrorOut",
    "SyncMcpToolsPayload",
    "SyncMcpToolsRequest",
    "SyncMcpToolsResult",
    "SyncMcpToolsResultOut",
    "SyncResult",
    "SyncedToolOut",
    "TOOL_REGISTRY_SCHEMA_SQL",
    "ToolConfigurationService",
    "ToolListResult",
    "ToolRecord",
    "ToolRegistrationView",
    "ToolRegistryService",
    "ToolRepository",
    "ToolVersionView",
    "ToolView",
    "UnregisterToolResult",
    "build_mcp_sync_router",
    "build_tools_router",
    "init_mcp_servers_schema",
    "init_tool_registry_schema",
    "init_tool_registry_schema_on_database",
    "mcp_server_to_view",
    "new_tool_id",
    "record_to_view",
    "record_to_version_view",
]
