"""模型配置应用接口；真实推理调用属于 Model Gateway。"""

from typing import Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class ModelConnectionService(Protocol):
    async def create_connection(
        self, actor: ActorContext, request: CreateModelConnectionRequest
    ) -> ModelConnectionView:
        """保存中转/供应商连接并安全存储 API Key。

        校验 URL、HTTPS/TLS 策略和 adapter 类型；把 Key 写入 SecretService，业务库只存
        secret_id；连接初始为 UNVERIFIED。失败时要回滚 Secret 或记录待清理项。响应只显示
        是否配置及指纹后四位，绝不能返回 Key。
        """
        ...

    async def list_connections(
        self, actor: ActorContext, query: ConnectionQuery
    ) -> ConnectionPage:
        """按权限返回脱敏连接列表，不解析 Secret。"""
        ...

    async def verify_connection(
        self, actor: ActorContext, connection_id: str, request: VerifyConnectionRequest
    ) -> ProbeResult:
        """按 DNS→TLS→认证→最小聊天的顺序探测连接。

        每级失败后不继续需要前级成功的检查；调用 ProviderAdapter，但不得把响应正文或密钥
        写日志。成功更新 READY，失败更新 ERROR 和脱敏摘要。重复幂等键返回原探测结果。
        """
        ...

    async def register_model(
        self, actor: ActorContext, connection_id: str, request: RegisterModelRequest
    ) -> ModelProfileView:
        """在已存在连接下登记远程模型名；不假设该模型具备 Tool/Schema 能力。"""
        ...

    async def probe_model(
        self, actor: ActorContext, profile_id: str, request: ProbeModelRequest
    ) -> ModelProfileView:
        """用低成本固定用例逐项探测能力并保存证据摘要。

        探测失败只把相应能力标为 false/unknown，不凭模型名称猜测。只有通过的能力才允许
        角色发布校验使用。
        """
        ...

    async def create_policy(
        self, actor: ActorContext, request: CreateModelPolicyRequest
    ) -> ModelPolicyView:
        """创建有顺序的主模型/fallback 策略。

        检查所有 Profile 存在、可用且没有重复；fallback 不会跨越角色授权。输出固定版本，
        后续修改应创建新版本而非影响已运行快照。
        """
        ...

    async def query_usage(self, actor: ActorContext, query: UsageQuery) -> UsagePage:
        """按调用者可见项目过滤用量，返回逐项记录和同一过滤条件下的聚合。"""
        ...

