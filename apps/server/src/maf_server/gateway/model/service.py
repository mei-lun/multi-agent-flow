"""解析策略、解密凭据、调用模型、预算记账和审计接口。"""

from typing import Protocol
from maf_contracts.common import ExecutionContext
from maf_contracts.model import *


class ModelGateway(Protocol):
    async def invoke(self, context: ExecutionContext, request: UnifiedModelRequest) -> UnifiedModelResponse:
        """在当前节点执行一次角色受限模型调用。

        顺序：校验 control commit、assignment epoch 和 call_key 幂等；确认 model_policy_id 在本地
        执行上下文授权内；
        预留预算；按 primary/fallback 顺序选择可用 Profile；调用 PolicyService；仅在调用前从
        SecretService 解析 Key；经 Adapter 调用；校验工具/结构化响应；记录 usage、费用和事件；
        提交或释放预算。只有归一化的可重试供应商错误才能 fallback，策略/预算错误不得换模型。
        """
        ...

    async def probe(self, request: ModelProbeRequest) -> dict:
        """供配置模块探测连接/能力；使用最小固定输入，不创建 Run 用量。"""
        ...

    async def get_call(self, context: ExecutionContext, call_id: str) -> ModelCallView:
        """仅允许同一 task/assignment 读取节点本地脱敏调用状态。"""
        ...

    async def cancel_call(
        self, context: ExecutionContext, call_id: str, request: CancelModelCallRequest
    ) -> ModelCallView:
        """幂等请求取消进行中调用；已结束调用返回当前终态，不能改写 usage。"""
        ...
