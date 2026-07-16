"""统一供应商 token、延迟、重试和估算费用的接口。"""

from typing import Protocol
from maf_contracts.model import ModelUsage


class UsageRecorder(Protocol):
    async def reserve(self, run_id: str, attempt_id: str, estimated_cost: str, tokens: int) -> str:
        """原子检查剩余预算并返回 reservation_id；不足时拒绝且不调用模型。"""
        ...

    async def commit(self, reservation_id: str, actual: ModelUsage) -> None:
        """用实际 usage 结算并写 cost ledger；重复提交必须幂等。"""
        ...

    async def release(self, reservation_id: str, reason: str) -> None:
        """调用未发生时释放预留；保留失败审计。"""
        ...

