"""IAM 公共 HTTP 接口签名；FastAPI 实现必须保持这些路径与语义。"""

from typing import Protocol

from maf_contracts.common import ActorContext

from .schemas import *


class IamHttpApi(Protocol):
    async def post_login(self, request: LoginRequest) -> SessionView:
        """POST `/api/v1/auth/login`；成功 200，认证失败 401。"""
        ...

    async def post_logout(self, actor: ActorContext, session_id: str) -> None:
        """POST `/api/v1/auth/logout`；幂等撤销当前 session，成功 204。"""
        ...

    async def get_me(self, actor: ActorContext) -> UserView:
        """GET `/api/v1/me`；返回当前用户和服务端重新计算的权限。"""
        ...

    async def get_users(self, actor: ActorContext, query: UserQuery) -> UserPage:
        """GET `/api/v1/users`；管理员游标分页查询，成功 200。"""
        ...

    async def post_user(self, actor: ActorContext, request: CreateUserRequest) -> UserView:
        """POST `/api/v1/users`；创建成功 201，用户名冲突 409。"""
        ...

    async def patch_user(
        self, actor: ActorContext, user_id: str, request: UpdateUserRequest
    ) -> UserView:
        """PATCH `/api/v1/users/{id}`；按版本部分更新，成功 200。"""
        ...

    async def get_setting(self, actor: ActorContext, key: str) -> SettingView:
        """GET `/api/v1/settings/{key}`；未知 key 返回 404。"""
        ...

    async def put_setting(
        self, actor: ActorContext, key: str, request: PutSettingRequest
    ) -> SettingView:
        """PUT `/api/v1/settings/{key}`；创建或替换设置，成功 200。"""
        ...

