"""站内待办公共 HTTP 接口（TASK-082）。

``build_inbox_router`` 工厂构造 FastAPI ``APIRouter``，挂载 inbox 路由：

- ``GET /api/v1/inbox``：列出当前用户可见的待办（可按 status/project_id 过滤）；
- ``GET /api/v1/inbox/{item_id}``：获取待办详情；
- ``POST /api/v1/inbox/{item_id}:decide``：人工决策（APPROVE/REJECT/
  REQUEST_CHANGES），决策成功 200，主题版本变化（并发决策）409；
- ``POST /api/v1/inbox/{item_id}:assign``：分配给指定用户（OWNER/ADMIN）；
- ``POST /api/v1/inbox/{item_id}:expire``：置为过期（系统调用）。

设计原则（与 ``reviews/router.py`` 一致）：
- router 是 service 的薄包装，只做 HTTP ↔ TypedDict 转换，不含业务逻辑；
- 使用 Pydantic 模型对外暴露 schema，内部用 ``ActorDep`` 注入认证上下文；
- 不暴露宿主绝对路径与内部 Repository 实现细节。
"""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import APIRouter, Body, Depends, Query, Request, status
from pydantic import BaseModel, Field
from typing_extensions import Annotated

from maf_contracts.common import ActorContext
from maf_server.api.dependencies import get_current_actor

from .schemas import (
    InboxDecision,
    InboxItemStatus,
    InboxItemType,
    InboxItemView,
    InboxPriority,
)


# --------------------------------------------------------------------------- #
# Pydantic 对外模型
# --------------------------------------------------------------------------- #


class InboxItemOut(BaseModel):
    """``InboxItemView`` 对外模型。"""

    id: str
    project_id: str
    title: str
    description: str
    item_type: InboxItemType
    artifact_id: str | None = None
    review_id: str | None = None
    assigned_to: str | None = None
    priority: InboxPriority
    status: InboxItemStatus
    decision: InboxDecision | None = None
    decision_comment: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    created_at: str
    created_by: str
    version_no: int = Field(ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _item_to_out(view: InboxItemView) -> InboxItemOut:
    """把 ``InboxItemView`` TypedDict 映射为 Pydantic ``InboxItemOut``。"""
    return InboxItemOut(
        id=view["id"],
        project_id=view["project_id"],
        title=view["title"],
        description=view["description"],
        item_type=view["item_type"],
        artifact_id=view["artifact_id"],
        review_id=view["review_id"],
        assigned_to=view["assigned_to"],
        priority=view["priority"],
        status=view["status"],
        decision=view["decision"],
        decision_comment=view["decision_comment"],
        decided_by=view["decided_by"],
        decided_at=view["decided_at"],
        created_at=view["created_at"],
        created_by=view["created_by"],
        version_no=view["version_no"],
        metadata=view["metadata"],
    )


class DecideInboxIn(BaseModel):
    """``POST /api/v1/inbox/{id}:decide`` 请求体。"""

    decision: InboxDecision
    comment: str = Field(min_length=1, max_length=8192, description="决策评论（必填）")
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssignInboxIn(BaseModel):
    """``POST /api/v1/inbox/{id}:assign`` 请求体。"""

    user_id: str = Field(min_length=1, description="被分配用户 ID")


# --------------------------------------------------------------------------- #
# Service 鸭子类型
# --------------------------------------------------------------------------- #


class InboxServiceLike(Protocol):
    """``build_inbox_router`` 接受的 inbox service 鸭子类型。"""

    async def create(
        self,
        request: dict[str, Any],
        *,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> InboxItemView: ...

    async def list_for_actor(
        self,
        actor_id: str,
        *,
        status: InboxItemStatus | None = None,
        project_id: str | None = None,
        actor: ActorContext | None = None,
    ) -> list[InboxItemView]: ...

    async def get(
        self,
        item_id: str,
        *,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> InboxItemView: ...

    async def decide(
        self,
        item_id: str,
        decision: dict[str, Any],
        *,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> InboxItemView: ...

    async def assign(
        self,
        item_id: str,
        user_id: str,
        *,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> InboxItemView: ...

    async def expire(
        self,
        item_id: str,
        *,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> InboxItemView: ...


# --------------------------------------------------------------------------- #
# actor 依赖占位（正式实现应在 api/dependencies.py）
# --------------------------------------------------------------------------- #


async def _anonymous_actor_dependency(request: Request) -> ActorContext:
    """占位 actor 依赖；正式实现应解析 Cookie/Authorization 构造 ActorContext。"""
    return await get_current_actor(request)


ActorDep = Annotated[ActorContext, Depends(_anonymous_actor_dependency)]


# --------------------------------------------------------------------------- #
# 路由工厂
# --------------------------------------------------------------------------- #


def build_inbox_router(inbox_service: InboxServiceLike) -> APIRouter:
    """构造 Inbox FastAPI ``APIRouter``。

    路由（TASK-082）：
        - ``GET /api/v1/inbox``：列出当前用户可见的待办；
        - ``GET /api/v1/inbox/{item_id}``：获取待办详情；
        - ``POST /api/v1/inbox/{item_id}:decide``：人工决策；
        - ``POST /api/v1/inbox/{item_id}:assign``：分配给指定用户；
        - ``POST /api/v1/inbox/{item_id}:expire``：置为过期。

    :param inbox_service: ``InboxService`` 实现。
    :returns: FastAPI ``APIRouter``，由调用方挂载到 app。
    """
    router = APIRouter(prefix="/api/v1/inbox", tags=["inbox"])

    @router.get(
        "",
        response_model=list[InboxItemOut],
        summary="列出当前用户的站内待办",
    )
    async def list_inbox(
        actor: ActorDep,
        status_filter: (
            InboxItemStatus | None
        ) = Query(default=None, alias="status"),
        project_id: str | None = Query(default=None),
    ) -> list[InboxItemOut]:
        """列出分配给当前用户或所有 APPROVER 可见的待办项。"""
        views = await inbox_service.list_for_actor(
            actor_id=actor.get("user_id", ""),
            status=status_filter,
            project_id=project_id,
            actor=actor,
        )
        return [_item_to_out(v) for v in views]

    @router.get(
        "/{item_id}",
        response_model=InboxItemOut,
        summary="获取待办详情",
    )
    async def get_inbox_item(
        actor: ActorDep,
        item_id: str,
    ) -> InboxItemOut:
        """按 item_id 获取待办详情。"""
        view = await inbox_service.get(
            item_id=item_id,
            actor_id=actor.get("user_id", ""),
            actor=actor,
        )
        return _item_to_out(view)

    @router.post(
        "/{item_id}:decide",
        response_model=InboxItemOut,
        status_code=status.HTTP_200_OK,
        summary="人工决策待办",
    )
    async def decide_inbox_item(
        actor: ActorDep,
        item_id: str,
        body: DecideInboxIn = Body(...),
    ) -> InboxItemOut:
        """对 PENDING 待办提交一次人工决策（APPROVE/REJECT/REQUEST_CHANGES）。

        决策成功返回 200；主题版本变化（并发决策）返回 409。
        """
        decision: dict[str, Any] = {
            "decision": body.decision,
            "comment": body.comment,
            "metadata": body.metadata,
        }
        view = await inbox_service.decide(
            item_id=item_id,
            decision=decision,
            actor_id=actor.get("user_id", ""),
            actor=actor,
        )
        return _item_to_out(view)

    @router.post(
        "/{item_id}:assign",
        response_model=InboxItemOut,
        summary="分配待办给指定用户",
    )
    async def assign_inbox_item(
        actor: ActorDep,
        item_id: str,
        body: AssignInboxIn = Body(...),
    ) -> InboxItemOut:
        """将待办分配给指定用户（需 manage inbox 权限，OWNER/ADMIN）。"""
        view = await inbox_service.assign(
            item_id=item_id,
            user_id=body.user_id,
            actor_id=actor.get("user_id", ""),
            actor=actor,
        )
        return _item_to_out(view)

    @router.post(
        "/{item_id}:expire",
        response_model=InboxItemOut,
        summary="将待办置为过期",
    )
    async def expire_inbox_item(
        actor: ActorDep,
        item_id: str,
    ) -> InboxItemOut:
        """将 PENDING 待办置为 EXPIRED（系统调用）。"""
        view = await inbox_service.expire(
            item_id=item_id,
            actor_id=actor.get("user_id", ""),
            actor=actor,
        )
        return _item_to_out(view)

    return router


__all__ = [
    "AssignInboxIn",
    "DecideInboxIn",
    "InboxItemOut",
    "InboxServiceLike",
    "build_inbox_router",
]
