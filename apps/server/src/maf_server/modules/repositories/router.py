"""仓库绑定 HTTP 接口与 FastAPI 路由实现。

TASK-035 范围：
- ``build_repositories_router(service)`` 工厂构造 FastAPI ``APIRouter``，挂载仓库
  绑定的 HTTP 端点。
- 端点路径：
    - ``POST   /api/v1/projects/{project_id}/repositories``      → bind_repository
    - ``GET    /api/v1/projects/{project_id}/repositories``       → list_bindings
    - ``POST   /api/v1/repositories/{binding_id}/verify``         → verify_binding
    - ``DELETE /api/v1/repositories/{binding_id}``                → remove_binding
- ``ActorDep`` 通过 ``X-MAF-Actor-ID`` 头注入 ``actor_id``。
- 领域错误由 ``register_error_handlers`` 统一映射为 HTTP 状态码。

保留 ``RepositoryHttpApi`` Protocol（TASK-083+ 接口契约）。
"""

from __future__ import annotations

from typing import Protocol

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, Field
from typing_extensions import Annotated

from maf_server.api.dependencies import get_current_actor_id

from .schemas import CredentialType


# --------------------------------------------------------------------------- #
# Pydantic 请求/响应模型
# --------------------------------------------------------------------------- #


class BindRepositoryPayload(BaseModel):
    """``POST /api/v1/projects/{id}/repositories`` 请求体。"""

    repository_url: str = Field(..., min_length=1, max_length=2048)
    branch: str = Field(..., min_length=1, max_length=256)
    credential_type: CredentialType = "NONE"
    credential_plaintext: str | None = Field(None, description="HTTPS_TOKEN 模式下的 token 明文")
    ssh_key_path: str | None = Field(None, description="SSH_KEY 模式下的 key 路径")


class RepositoryBindingOut(BaseModel):
    """仓库绑定响应模型。"""

    id: str
    project_id: str
    repository_url: str
    branch: str
    credential_type: CredentialType
    credential_configured: bool
    verified: bool
    verified_at: str | None = None
    bound_by: str
    bound_at: str
    version: int


class RepositoryBindingListOut(BaseModel):
    """``GET /api/v1/projects/{id}/repositories`` 响应体。"""

    items: list[RepositoryBindingOut]


class ErrorResponse(BaseModel):
    """对外错误响应体。"""

    error_code: str
    message: str
    retryable: bool = False


# --------------------------------------------------------------------------- #
# Actor 依赖（开发期 stub）
# --------------------------------------------------------------------------- #


async def _actor_id_dependency(
    request: Request,
    x_maf_actor_id: str | None = Header(default=None),
) -> str:
    """从 ``X-MAF-Actor-ID`` 头读取 ``actor_id``。"""
    if x_maf_actor_id:
        return x_maf_actor_id
    try:
        return await get_current_actor_id(request)
    except Exception:
        return ""


ActorDep = Annotated[str, Depends(_actor_id_dependency)]


# --------------------------------------------------------------------------- #
# 视图转换辅助
# --------------------------------------------------------------------------- #


def _binding_to_out(view: dict) -> RepositoryBindingOut:
    return RepositoryBindingOut(
        id=view["id"],
        project_id=view["project_id"],
        repository_url=view["repository_url"],
        branch=view["branch"],
        credential_type=view["credential_type"],
        credential_configured=view["credential_configured"],
        verified=view["verified"],
        verified_at=view.get("verified_at"),
        bound_by=view["bound_by"],
        bound_at=view["bound_at"],
        version=view["version"],
    )


# --------------------------------------------------------------------------- #
# TASK-083+ 占位 Protocol（保留）
# --------------------------------------------------------------------------- #


from maf_contracts.common import ActorContext  # noqa: E402
from .schemas import (  # noqa: E402
    MergeRepositoryChangeRequest,
    MergeResultView,
    RepositoryChangeView,
    RepositoryHealth,
    VerifyRepositoryRequest,
)


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


# --------------------------------------------------------------------------- #
# TASK-035: 服务类型协议
# --------------------------------------------------------------------------- #


class RepositoryBindingServiceLike(Protocol):
    """``RepositoryBindingServiceImpl`` 的结构化类型协议。"""

    async def bind_repository(
        self,
        project_id: str,
        repository_url: str,
        branch: str,
        *,
        credential_type: str = ...,
        credential_plaintext: str | None = ...,
        ssh_key_path: str | None = ...,
        actor_id: str,
    ) -> dict:
        ...

    async def verify_binding(self, binding_id: str, *, actor_id: str) -> dict:
        ...

    async def list_bindings(self, project_id: str, *, actor_id: str) -> list[dict]:
        ...

    async def remove_binding(self, binding_id: str, *, actor_id: str) -> None:
        ...


# --------------------------------------------------------------------------- #
# FastAPI Router 工厂
# --------------------------------------------------------------------------- #


def build_repositories_router(service: RepositoryBindingServiceLike) -> APIRouter:
    """构造仓库绑定 FastAPI ``APIRouter``。

    :param service: ``RepositoryBindingServiceImpl`` 或任何满足签名协议的对象。
    :returns: FastAPI ``APIRouter``，由调用方挂载到 app。
    """
    router = APIRouter(tags=["repositories"])

    # ------------------------------------------------------------------ #
    # 仓库绑定 CRUD
    # ------------------------------------------------------------------ #

    @router.post(
        "/api/v1/projects/{project_id}/repositories",
        response_model=RepositoryBindingOut,
        status_code=status.HTTP_201_CREATED,
        responses={
            400: {"model": ErrorResponse, "description": "参数错误"},
            403: {"model": ErrorResponse, "description": "权限不足"},
            404: {"model": ErrorResponse, "description": "项目不存在"},
        },
    )
    async def bind_repository(  # noqa: ANN202
        project_id: str,
        payload: BindRepositoryPayload,
        actor_id: ActorDep,
    ) -> RepositoryBindingOut:
        """绑定仓库到项目。"""
        view = await service.bind_repository(
            project_id,
            payload.repository_url,
            payload.branch,
            credential_type=payload.credential_type,
            credential_plaintext=payload.credential_plaintext,
            ssh_key_path=payload.ssh_key_path,
            actor_id=actor_id,
        )
        return _binding_to_out(view)

    @router.get(
        "/api/v1/projects/{project_id}/repositories",
        response_model=RepositoryBindingListOut,
        responses={
            403: {"model": ErrorResponse, "description": "权限不足"},
            404: {"model": ErrorResponse, "description": "项目不存在"},
        },
    )
    async def list_bindings(  # noqa: ANN202
        project_id: str,
        actor_id: ActorDep,
    ) -> RepositoryBindingListOut:
        """列出项目的仓库绑定。"""
        items = await service.list_bindings(project_id, actor_id=actor_id)
        return RepositoryBindingListOut(items=[_binding_to_out(v) for v in items])

    @router.post(
        "/api/v1/repositories/{binding_id}/verify",
        response_model=RepositoryBindingOut,
        responses={
            403: {"model": ErrorResponse, "description": "权限不足"},
            404: {"model": ErrorResponse, "description": "绑定不存在"},
        },
    )
    async def verify_binding(  # noqa: ANN202
        binding_id: str,
        actor_id: ActorDep,
    ) -> RepositoryBindingOut:
        """验证仓库绑定。"""
        view = await service.verify_binding(binding_id, actor_id=actor_id)
        return _binding_to_out(view)

    @router.delete(
        "/api/v1/repositories/{binding_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_model=None,
        responses={
            403: {"model": ErrorResponse, "description": "权限不足"},
            404: {"model": ErrorResponse, "description": "绑定不存在"},
        },
    )
    async def remove_binding(  # noqa: ANN202
        binding_id: str,
        actor_id: ActorDep,
    ) -> None:
        """移除仓库绑定。"""
        await service.remove_binding(binding_id, actor_id=actor_id)

    return router


__all__ = [
    "RepositoryHttpApi",
    "RepositoryBindingServiceLike",
    "build_repositories_router",
    "ActorDep",
    "RepositoryBindingOut",
    "RepositoryBindingListOut",
    "BindRepositoryPayload",
    "ErrorResponse",
]
