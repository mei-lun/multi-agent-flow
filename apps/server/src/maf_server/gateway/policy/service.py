"""Model、Skill、Tool、Path、Network 的 fail-closed 授权接口。"""

from typing import Any, Protocol
from maf_contracts.tool import ToolCallRequest
from maf_server.modules.tools.schemas import CapabilityDecisionView


class CapabilityPolicyService(Protocol):
    async def evaluate(
        self, subject: dict[str, Any], action: str, resource: str, context: dict[str, Any]
    ) -> CapabilityDecisionView:
        """组合 PyCasbin 和参数级验证器。

        先验证上下文字段完整且来自服务端；Casbin 判断主体/资源/动作；再对 Tool 参数、路径、
        URL、网络、预算和资源上限做收紧；返回 decision_id、理由、约束后参数、审批要求和
        obligations。异常、超时、未知策略版本或验证器缺失都返回 allowed=false。
        """
        ...
