"""模型配置公共 HTTP 接口。"""

from typing import Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class ModelConnectionHttpApi(Protocol):
    async def post_connection(self, actor: ActorContext, request: CreateModelConnectionRequest) -> ModelConnectionView:
        """POST `/api/v1/model-connections`；成功 201，响应永不含明文 Key。"""
        ...
    async def get_connections(self, actor: ActorContext, query: ConnectionQuery) -> ConnectionPage:
        """GET `/api/v1/model-connections`；脱敏分页列表。"""
        ...
    async def post_verify(self, actor: ActorContext, connection_id: str, request: VerifyConnectionRequest) -> ProbeResult:
        """POST `/api/v1/model-connections/{id}/verify`；完成探测返回 200。"""
        ...
    async def post_model(self, actor: ActorContext, connection_id: str, request: RegisterModelRequest) -> ModelProfileView:
        """POST `/api/v1/model-connections/{id}/models`；登记成功 201。"""
        ...
    async def post_probe(self, actor: ActorContext, profile_id: str, request: ProbeModelRequest) -> ModelProfileView:
        """POST `/api/v1/model-profiles/{id}/probe`；返回能力矩阵。"""
        ...
    async def post_policy(self, actor: ActorContext, request: CreateModelPolicyRequest) -> ModelPolicyView:
        """POST `/api/v1/model-policies`；创建成功 201。"""
        ...
    async def get_usage(self, actor: ActorContext, query: UsageQuery) -> UsagePage:
        """GET `/api/v1/model-usage`；按项目权限过滤。"""
        ...

