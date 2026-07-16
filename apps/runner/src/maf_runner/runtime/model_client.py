"""通过节点本地 Model Gateway 和本地 SecretStore 调用供应商的接口。"""

from typing import Protocol
from maf_contracts.model import UnifiedModelRequest, UnifiedModelResponse


class ModelClient(Protocol):
    async def invoke(self, request: UnifiedModelRequest) -> UnifiedModelResponse:
        """校验任务授权的模型别名后调用节点本地 Adapter；Key 只从本机 SecretStore 解析。

        call_key 在本地逻辑重试时保持不变；用量写节点本地数据库并在脱敏提交报告中汇总。
        """
        ...
    async def cancel(self, call_id: str, reason: str) -> None:
        """任务取消或 assignment epoch 失效时取消本地供应商调用；随后不再消费流。"""
        ...
