"""Project 模块 HTTP 接口与 FastAPI 路由实现。

TASK-033 范围：
- ``build_projects_router(service)`` 工厂构造 FastAPI ``APIRouter``，挂载项目 CRUD
  与成员管理的 HTTP 端点。
- 端点路径对齐《接口设计与实现规范》：
    - ``POST   /api/v1/projects``                  → create_project
    - ``GET    /api/v1/projects``                   → list_projects
    - ``GET    /api/v1/projects/{project_id}``      → get_project
    - ``PATCH  /api/v1/projects/{project_id}``      → update_project
    - ``DELETE /api/v1/projects/{project_id}``      → delete_project
    - ``GET    /api/v1/projects/{project_id}/members``  → list_members
    - ``POST   /api/v1/projects/{project_id}/members``  → add_member
    - ``DELETE /api/v1/projects/{project_id}/members/{user_id}`` → remove_member
    - ``PATCH  /api/v1/projects/{project_id}/members/{user_id}`` → update_member_role

- ``ActorDep`` 通过 ``X-MAF-Actor-ID`` 头注入 ``actor_id``；正式认证由后续任务的
  认证中间件负责，本任务仅提供开发期 stub。
- 领域错误由 ``register_error_handlers`` 统一映射为 HTTP 状态码（本路由不重复处理）。
"""

from __future__ import annotations

from typing import Protocol

from fastapi import APIRouter, Depends, Header, status
from pydantic import BaseModel, Field
from typing_extensions import Annotated

from .schemas import (
    ProjectMemberRole,
    ProjectMemberView,
    ProjectStatus,
    ProjectView,
)


# --------------------------------------------------------------------------- #
# Pydantic 请求/响应模型（与 schemas.TypedDict 对齐，供 FastAPI 序列化）
# --------------------------------------------------------------------------- #


class ProjectOut(BaseModel):
    """项目响应模型。"""

    id: str
    name: str
    description: str
    status: ProjectStatus
    created_at: str
    created_by: str
    updated_at: str
    version: int
    deleted_at: str | None = None


class MemberOut(BaseModel):
    """项目成员响应模型。"""

    project_id: str
    user_id: str
    role: ProjectMemberRole
    added_at: str
    added_by: str
    version: int


class CreateProjectPayload(BaseModel):
    """``POST /api/v1/projects`` 请求体。"""

    name: str = Field(..., min_length=1, max_length=128, description="项目名称")
    description: str = Field("", max_length=4096, description="项目描述")


class UpdateProjectPayload(BaseModel):
    """``PATCH /api/v1/projects/{id}`` 请求体。"""

    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = Field(None, max_length=4096)
    status: ProjectStatus | None = None
    expected_version: int = Field(..., ge=1, description="乐观锁期望版本号")


class AddMemberPayload(BaseModel):
    """``POST /api/v1/projects/{id}/members`` 请求体。"""

    user_id: str = Field(..., min_length=1)
    role: ProjectMemberRole


class UpdateMemberRolePayload(BaseModel):
    """``PATCH /api/v1/projects/{id}/members/{user_id}`` 请求体。"""

    new_role: ProjectMemberRole


class ProjectListOut(BaseModel):
    """``GET /api/v1/projects`` 响应体。"""

    items: list[ProjectOut]
    next_cursor: str | None = None
    has_more: bool = False


class MemberListOut(BaseModel):
    """``GET /api/v1/projects/{id}/members`` 响应体。"""

    items: list[MemberOut]


class ErrorResponse(BaseModel):
    """对外错误响应体（精简版，与 ``api.errors.ErrorResponse`` 对齐）。"""

    error_code: str
    message: str
    retryable: bool = False


# --------------------------------------------------------------------------- #
# Actor 依赖（开发期 stub；正式认证由后续任务中间件负责）
# --------------------------------------------------------------------------- #


def _actor_id_dependency(x_maf_actor_id: str | None = Header(default=None)) -> str:
    """从 ``X-MAF-Actor-ID`` 头读取 ``actor_id``。

    TASK-033 仅提供开发期 stub；正式认证中间件将在后续任务实现，构造
    ``ActorContext`` 并注入到路由。本依赖仅返回 ``actor_id`` 字符串，由 service
    层 internally 构建 ``ActorContext``。
    """
    if not x_maf_actor_id:
        # 开发期允许匿名调用是危险的；这里返回空串由 service 层拒绝。
        # 正式中间件应在此返回 401。
        return ""
    return x_maf_actor_id


ActorDep = Annotated[str, Depends(_actor_id_dependency)]


# --------------------------------------------------------------------------- #
# Protocol（保留接口契约）
# --------------------------------------------------------------------------- #


class ProjectHttpApi(Protocol):
    """Project HTTP API 接口契约（供后续任务扩展）。"""

    async def post_project(
        self, actor_id: str, request: CreateProjectPayload
    ) -> ProjectOut:
        ...


# --------------------------------------------------------------------------- #
# 视图转换辅助
# --------------------------------------------------------------------------- #


def _project_to_out(view: ProjectView) -> ProjectOut:
    return ProjectOut(
        id=view["id"],
        name=view["name"],
        description=view["description"],
        status=view["status"],
        created_at=view["created_at"],
        created_by=view["created_by"],
        updated_at=view["updated_at"],
        version=view["version"],
        deleted_at=view.get("deleted_at"),
    )


def _member_to_out(view: ProjectMemberView) -> MemberOut:
    return MemberOut(
        project_id=view["project_id"],
        user_id=view["user_id"],
        role=view["role"],
        added_at=view["added_at"],
        added_by=view["added_by"],
        version=view["version"],
    )


# --------------------------------------------------------------------------- #
# FastAPI Router 工厂
# --------------------------------------------------------------------------- #


def build_projects_router(service: "ProjectApplicationServiceLike") -> APIRouter:
    """构造 Project FastAPI ``APIRouter``。

    :param service: ``ProjectApplicationServiceImpl`` 或任何满足签名协议的对象。
    :returns: FastAPI ``APIRouter``，由调用方挂载到 app。
    """
    router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

    # ------------------------------------------------------------------ #
    # 项目 CRUD
    # ------------------------------------------------------------------ #

    @router.post(
        "",
        response_model=ProjectOut,
        status_code=status.HTTP_201_CREATED,
        responses={
            400: {"model": ErrorResponse, "description": "参数错误"},
            403: {"model": ErrorResponse, "description": "权限不足"},
        },
    )
    async def create_project(  # noqa: ANN202
        payload: CreateProjectPayload,
        actor_id: ActorDep,
    ) -> ProjectOut:
        """创建项目；creator 自动成为 OWNER。"""
        view = await service.create_project(
            payload.name, payload.description, actor_id=actor_id
        )
        return _project_to_out(view)

    @router.get(
        "",
        response_model=ProjectListOut,
        responses={
            403: {"model": ErrorResponse, "description": "权限不足"},
        },
    )
    async def list_projects(  # noqa: ANN202
        actor_id: ActorDep,
    ) -> ProjectListOut:
        """返回调用者可见项目（成员关系过滤）。"""
        items = await service.list_projects(actor_id=actor_id)
        return ProjectListOut(
            items=[_project_to_out(v) for v in items],
            next_cursor=None,
            has_more=False,
        )

    @router.get(
        "/{project_id}",
        response_model=ProjectOut,
        responses={
            403: {"model": ErrorResponse, "description": "权限不足"},
            404: {"model": ErrorResponse, "description": "项目不存在"},
        },
    )
    async def get_project(  # noqa: ANN202
        project_id: str,
        actor_id: ActorDep,
    ) -> ProjectOut:
        """读取项目详情（需为项目成员）。"""
        view = await service.get_project(project_id, actor_id=actor_id)
        return _project_to_out(view)

    @router.patch(
        "/{project_id}",
        response_model=ProjectOut,
        responses={
            400: {"model": ErrorResponse, "description": "参数错误"},
            403: {"model": ErrorResponse, "description": "权限不足"},
            404: {"model": ErrorResponse, "description": "项目不存在"},
            409: {"model": ErrorResponse, "description": "版本冲突"},
        },
    )
    async def update_project(  # noqa: ANN202
        project_id: str,
        payload: UpdateProjectPayload,
        actor_id: ActorDep,
    ) -> ProjectOut:
        """乐观锁更新项目。"""
        view = await service.update_project(
            project_id,
            name=payload.name,
            description=payload.description,
            status=payload.status,
            expected_version=payload.expected_version,
            actor_id=actor_id,
        )
        return _project_to_out(view)

    @router.delete(
        "/{project_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_model=None,
        responses={
            403: {"model": ErrorResponse, "description": "权限不足"},
            404: {"model": ErrorResponse, "description": "项目不存在"},
            409: {"model": ErrorResponse, "description": "版本冲突"},
        },
    )
    async def delete_project(  # noqa: ANN202
        project_id: str,
        expected_version: int,
        actor_id: ActorDep,
    ) -> None:
        """软删除项目。``expected_version`` 通过查询参数传入。"""
        await service.delete_project(
            project_id, expected_version, actor_id=actor_id
        )

    # ------------------------------------------------------------------ #
    # 成员管理
    # ------------------------------------------------------------------ #

    @router.get(
        "/{project_id}/members",
        response_model=MemberListOut,
        responses={
            403: {"model": ErrorResponse, "description": "权限不足"},
            404: {"model": ErrorResponse, "description": "项目不存在"},
        },
    )
    async def list_members(  # noqa: ANN202
        project_id: str,
        actor_id: ActorDep,
    ) -> MemberListOut:
        """列出项目成员。"""
        items = await service.list_members(project_id, actor_id=actor_id)
        return MemberListOut(items=[_member_to_out(m) for m in items])

    @router.post(
        "/{project_id}/members",
        response_model=MemberOut,
        status_code=status.HTTP_201_CREATED,
        responses={
            400: {"model": ErrorResponse, "description": "参数错误"},
            403: {"model": ErrorResponse, "description": "权限不足"},
            404: {"model": ErrorResponse, "description": "项目不存在"},
            409: {"model": ErrorResponse, "description": "成员已存在"},
        },
    )
    async def add_member(  # noqa: ANN202
        project_id: str,
        payload: AddMemberPayload,
        actor_id: ActorDep,
    ) -> MemberOut:
        """添加项目成员。"""
        view = await service.add_member(
            project_id, payload.user_id, payload.role, actor_id=actor_id
        )
        return _member_to_out(view)

    @router.delete(
        "/{project_id}/members/{user_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_model=None,
        responses={
            403: {"model": ErrorResponse, "description": "权限不足"},
            404: {"model": ErrorResponse, "description": "成员不存在"},
            400: {"model": ErrorResponse, "description": "不能移除最后一个 OWNER"},
        },
    )
    async def remove_member(  # noqa: ANN202
        project_id: str,
        user_id: str,
        actor_id: ActorDep,
    ) -> None:
        """移除项目成员（最后 OWNER 保护）。"""
        await service.remove_member(project_id, user_id, actor_id=actor_id)

    @router.patch(
        "/{project_id}/members/{user_id}",
        response_model=MemberOut,
        responses={
            400: {"model": ErrorResponse, "description": "参数错误或最后 OWNER 保护"},
            403: {"model": ErrorResponse, "description": "权限不足"},
            404: {"model": ErrorResponse, "description": "成员不存在"},
            409: {"model": ErrorResponse, "description": "版本冲突"},
        },
    )
    async def update_member_role(  # noqa: ANN202
        project_id: str,
        user_id: str,
        payload: UpdateMemberRolePayload,
        actor_id: ActorDep,
    ) -> MemberOut:
        """变更成员角色（最后 OWNER 保护）。"""
        view = await service.update_member_role(
            project_id, user_id, payload.new_role, actor_id=actor_id
        )
        return _member_to_out(view)

    return router


# --------------------------------------------------------------------------- #
# 服务类型协议（structural typing，避免运行时依赖）
# --------------------------------------------------------------------------- #


class ProjectApplicationServiceLike(Protocol):
    """``ProjectApplicationServiceImpl`` 的结构化类型协议，供 router 类型提示。"""

    async def create_project(
        self, name: str, description: str, *, actor_id: str
    ) -> ProjectView:
        ...

    async def get_project(
        self, project_id: str, *, actor_id: str
    ) -> ProjectView:
        ...

    async def list_projects(self, *, actor_id: str) -> list[ProjectView]:
        ...

    async def update_project(
        self,
        project_id: str,
        *,
        name: str | None = ...,
        description: str | None = ...,
        status: str | None = ...,
        expected_version: int,
        actor_id: str,
    ) -> ProjectView:
        ...

    async def delete_project(
        self, project_id: str, expected_version: int, *, actor_id: str
    ) -> None:
        ...

    async def add_member(
        self,
        project_id: str,
        user_id: str,
        role: str,
        *,
        actor_id: str,
    ) -> ProjectMemberView:
        ...

    async def remove_member(
        self, project_id: str, user_id: str, *, actor_id: str
    ) -> None:
        ...

    async def list_members(
        self, project_id: str, *, actor_id: str
    ) -> list[ProjectMemberView]:
        ...

    async def update_member_role(
        self,
        project_id: str,
        user_id: str,
        new_role: str,
        *,
        actor_id: str,
    ) -> ProjectMemberView:
        ...


__all__ = [
    "ProjectHttpApi",
    "ProjectApplicationServiceLike",
    "build_projects_router",
    "ActorDep",
    "ProjectOut",
    "MemberOut",
    "CreateProjectPayload",
    "UpdateProjectPayload",
    "AddMemberPayload",
    "UpdateMemberRolePayload",
    "ProjectListOut",
    "MemberListOut",
    "ErrorResponse",
]
