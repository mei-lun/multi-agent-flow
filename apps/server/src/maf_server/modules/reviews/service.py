"""Review 查询与确定性 Quality Gate 接口。"""

from typing import Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class ReviewService(Protocol):
    async def list_reviews(self, actor: ActorContext, query: ReviewQuery) -> ReviewPage:
        """按项目权限和查询条件分页返回评审，证据只返回 Artifact 引用。"""
        ...


class QualityGateService(Protocol):
    async def evaluate(self, gate: GateDefinition, inputs: GateInputs) -> GateDecisionView:
        """依据固定规则汇总 Artifact 校验和 Review。

        检查所需报告存在且属于当前 Run/节点；存在阻断项时按类别选择明确返工目标；材料
        缺失且可人工补充时 WAITING_HUMAN；全部规则满足才 PASS。不得让 LLM 自行决定是否
        忽略阻断项；相同输入必须得到相同决策。
        """
        ...

