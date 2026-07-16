"""Tool 配置公共 HTTP 接口。"""

from typing import Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class ToolConfigurationHttpApi(Protocol):
    async def post_tool(self, actor: ActorContext, request: RegisterToolRequest) -> ToolView:
        """POST `/api/v1/tools`；注册成功 201。"""
        ...
    async def post_sync_mcp(self, actor: ActorContext, mcp_server_id: str, request: SyncMcpToolsRequest) -> SyncMcpToolsResult:
        """POST `/api/v1/mcp-servers/{id}/sync-tools`；同步完成 200。"""
        ...
    async def post_policy_simulation(self, actor: ActorContext, request: PolicySimulationRequest) -> CapabilityDecisionView:
        """POST `/api/v1/policies/simulate`；只返回决策，不产生外部动作。"""
        ...

