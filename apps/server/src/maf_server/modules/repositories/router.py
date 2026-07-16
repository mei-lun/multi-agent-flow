"""仓库公共 HTTP 接口。"""

from typing import Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class RepositoryHttpApi(Protocol):
    async def post_verify(self, actor: ActorContext, binding_id: str, request: VerifyRepositoryRequest) -> RepositoryHealth:
        """POST `/api/v1/repositories/{id}/verify`；验证完成返回 200。"""
        ...
    async def get_run_change(self, actor: ActorContext, run_id: str) -> RepositoryChangeView:
        """GET `/api/v1/runs/{id}/repository-change`；成功 200。"""
        ...
    async def post_merge(self, actor: ActorContext, change_id: str, request: MergeRepositoryChangeRequest) -> MergeResultView:
        """POST `/api/v1/repository-changes/{id}:merge`；只接受最终门禁通过的命令。"""
        ...

