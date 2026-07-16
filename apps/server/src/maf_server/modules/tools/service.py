"""Tool 注册与策略配置接口；实际调用位于 Tool Gateway。"""

from typing import Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class ToolConfigurationService(Protocol):
    async def register_tool(self, actor: ActorContext, request: RegisterToolRequest) -> ToolView:
        """注册工具元数据而不执行工具。

        验证稳定 key、输入输出 JSON Schema、adapter 引用、风险等级、超时和审批模式；
        NATIVE key 必须已在白名单注册，HTTP URL 必须符合网络策略，MCP 必须来自已配置
        Server。保存版本并写审计事件。
        """
        ...

    async def sync_mcp_tools(
        self, actor: ActorContext, mcp_server_id: str, request: SyncMcpToolsRequest
    ) -> SyncMcpToolsResult:
        """从 MCP Server 读取能力列表并转成 Tool Definition。

        先探测连接，再获取工具及 Schema；按 MCP server + remote key 幂等 upsert；远端缺失
        工具只有在 replace_missing=true 时禁用，不能删除历史版本。不得在同步时执行工具。
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

