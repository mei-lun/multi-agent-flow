"""Role 公共 HTTP 接口。"""

from typing import Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class RoleHttpApi(Protocol):
    async def post_role(self, actor: ActorContext, request: CreateRoleRequest) -> RoleView:
        """POST `/api/v1/roles`；创建 Definition，成功 201。"""
        ...
    async def post_version(self, actor: ActorContext, role_id: str, request: CreateRoleVersionRequest) -> RoleVersionView:
        """POST `/api/v1/roles/{id}/versions`；创建 DRAFT，成功 201。"""
        ...
    async def post_dry_run(self, actor: ActorContext, version_id: str, request: DryRunRoleRequest) -> DryRunRoleResult:
        """POST `/api/v1/role-versions/{id}/dry-run`；异步受理返回 202。"""
        ...
    async def post_publish(self, actor: ActorContext, version_id: str, request: PublishRoleRequest) -> RoleVersionView:
        """POST `/api/v1/role-versions/{id}/publish`；发布成功 200。"""
        ...

