"""站内待办创建、查询与人工决策接口（TASK-082）。

实现 ``InboxServiceImpl``，提供 Inbox 人工决策服务：

- ``create``：系统创建待办项（如 QualityGate 评估后需要人工审批），不需权限
  检查，产生 ``inbox.item_created`` 事件；
- ``list_for_actor``：列出当前用户可见的待办项（分配给该用户或所有 APPROVER
  可见），需 ``read inbox`` 权限；
- ``get``：获取待办详情，需 ``read inbox`` 权限；
- ``decide``：人工决策（APPROVE/REJECT/REQUEST_CHANGES），状态 PENDING →
  DECIDED，需 ``write inbox`` 权限（APPROVER/ADMIN），且只有 assignee/管理员
  可决定；如关联 ``review_id`` 则触发 ReviewService 对应方法；产生
  ``inbox.item_decided`` 事件；
- ``assign``：分配给指定用户，需 ``manage inbox`` 权限（OWNER/ADMIN）；
- ``expire``：系统调用，将待办置为 EXPIRED。

权限检查通过 ``PermissionService.require``（对应任务目标 4）。
事件经 ``SqliteEventPublisher`` 写入 Outbox（对应任务目标 6）。
与 ReviewService 集成：``decide`` 在 inbox UoW 提交后调用 ReviewService 的
approve/reject/request_changes（对应任务目标 5）。

事务边界：
- 写操作（create/decide/assign/expire）：UoW 内写入 + 事件 commit；
- ReviewService 调用在 inbox UoW 提交后进行（ReviewService 自开 UoW，避免
  嵌套持锁死锁）；
- 读操作（list_for_actor/get）：UoW 内只读（rollback）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from maf_contracts.common import ActorContext
from maf_contracts.events import ActorRef, DomainEvent
from maf_domain.errors import (
    ArgumentError,
    NotFoundError,
    PermissionDeniedError,
    UnauthenticatedError,
    UnsupportedOperationError,
    VersionConflictError,
)
from maf_policy import CasbinPermissionService
from maf_server.core.database import Database
from maf_server.core.events import SqliteEventPublisher
from maf_server.core.unit_of_work import SqliteUnitOfWork

from .repository import (
    InboxItemRecord,
    SqliteInboxRepository,
    new_inbox_item_id,
    record_to_view,
)
from .schemas import (
    CreateInboxRequest,
    DecideRequest,
    InboxDecision,
    InboxItemStatus,
    InboxItemType,
    InboxItemView,
    InboxPriority,
)

# --------------------------------------------------------------------------- #
# 资源与动作常量（与 DEFAULT_POLICIES 中的资源/动作命名对齐）
# --------------------------------------------------------------------------- #

RESOURCE_INBOX: str = "inbox"
ACTION_READ: str = "read"
ACTION_WRITE: str = "write"
ACTION_MANAGE: str = "manage"

#: 管理员角色（可决定任意 assignee 的待办）。
_ADMIN_ROLE: str = "ADMIN"

# --------------------------------------------------------------------------- #
# 内部时钟（与 reviews.service._SystemClock 对齐）
# --------------------------------------------------------------------------- #


class _SystemClock:
    """默认使用系统 UTC 时钟；测试可注入虚拟时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #


def _ensure_iso(value: datetime) -> str:
    """把 datetime 序列化为带时区 ISO 8601 字符串。naive 视为 UTC。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _require_actor(actor: ActorContext) -> str:
    """校验 actor 并返回 user_id。未认证抛 ``UnauthenticatedError``。"""
    if not isinstance(actor, dict):
        raise UnauthenticatedError("未认证")
    user_id = actor.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        raise UnauthenticatedError("未认证")
    return user_id


def _actor_context(
    actor_id: str, actor: ActorContext | None
) -> tuple[ActorContext, str, str, str]:
    """构造/补全 actor 上下文，返回 (ctx, user_id, org_id, trace_id)。"""
    actor_ctx = actor or {
        "user_id": actor_id,
        "organization_id": "system",
        "permission_keys": [],
        "trace_id": "",
    }
    user_id = _require_actor(actor_ctx)
    org_id = (
        actor_ctx.get("organization_id")
        if isinstance(actor_ctx, dict)
        else "system"
    )
    if not isinstance(org_id, str) or not org_id:
        org_id = "system"
    trace_id = (
        actor_ctx.get("trace_id") if isinstance(actor_ctx, dict) else ""
    )
    if not isinstance(trace_id, str):
        trace_id = ""
    return actor_ctx, user_id, org_id, trace_id


def _is_admin(actor: ActorContext) -> bool:
    """判断 actor 是否持有 ADMIN 角色。"""
    if not isinstance(actor, dict):
        return False
    keys = actor.get("permission_keys")
    if not isinstance(keys, list):
        return False
    return _ADMIN_ROLE in keys


_VALID_ITEM_TYPES = frozenset(
    {"REVIEW_REQUEST", "CHANGE_REQUEST", "APPROVAL_REQUEST"}
)
_VALID_DECISIONS = frozenset({"APPROVE", "REJECT", "REQUEST_CHANGES"})
_VALID_PRIORITIES = frozenset({"LOW", "NORMAL", "HIGH", "URGENT"})
_VALID_STATUSES = frozenset({"PENDING", "DECIDED", "EXPIRED"})


# --------------------------------------------------------------------------- #
# ReviewService 鸭子类型（避免跨模块导入循环）
# --------------------------------------------------------------------------- #


class _ReviewServiceLike(Protocol):
    """``InboxServiceImpl.decide`` 调用的 ReviewService 鸭子类型。

    与 ``maf_server.modules.reviews.service.ArtifactReviewServiceImpl`` 对齐，
    只需 ``approve_review``/``reject_review``/``request_changes`` 三个方法。
    """

    async def approve_review(
        self, review_id: str, *, actor_id: str, comment: str,
        actor: ActorContext | None = None,
    ) -> Any: ...

    async def reject_review(
        self, review_id: str, *, actor_id: str, comment: str,
        actor: ActorContext | None = None,
    ) -> Any: ...

    async def request_changes(
        self, review_id: str, *, actor_id: str, comment: str,
        actor: ActorContext | None = None,
    ) -> Any: ...


# --------------------------------------------------------------------------- #
# PermissionService Protocol（与 reviews.service.PermissionService 对齐）
# --------------------------------------------------------------------------- #


class PermissionService(Protocol):
    """权限检查协议（与 ``maf_policy.CasbinPermissionService`` 对齐）。"""

    async def require(
        self, actor: ActorContext, action: str, resource: str
    ) -> None: ...


# --------------------------------------------------------------------------- #
# InboxServiceImpl 具体实现
# --------------------------------------------------------------------------- #


class InboxServiceImpl:
    """Inbox 人工决策服务实现（TASK-082）。

    依赖注入：
        - ``database``：``Database``，提供 ``SqliteUnitOfWork`` 事务边界；
        - ``repository``：``SqliteInboxRepository``，待办项 CRUD；
        - ``permission_service``：``PermissionService``，默认
          ``CasbinPermissionService``；
        - ``review_service``：可选的 ``ReviewService``，``decide`` 时据此触发
          评审状态转换；
        - ``clock``：可注入虚拟时钟用于测试。

    权限检查（对应任务目标 4）：
        - ``create``：系统创建，不需权限检查（QualityGate 等系统模块可调用）；
        - ``list_for_actor``/``get``：``require(actor, "read", "inbox")``；
        - ``decide``：``require(actor, "write", "inbox")``（APPROVER/ADMIN），
          且只有 assignee/管理员可决定（对应验收：只有 assignee/管理员可决定）；
        - ``assign``：``require(actor, "manage", "inbox")``（OWNER/ADMIN）；
        - ``expire``：系统调用，不需权限检查。

    可见性规则（对应任务目标 2）：
        - ``list_for_actor`` 返回 ``assigned_to == actor_id`` 或
          ``assigned_to IS NULL``（所有 APPROVER 可见）的项。

    与 ReviewService 集成（对应任务目标 5）：
        - ``decide`` APPROVE + review_id → ``review_service.approve_review``；
        - ``decide`` REJECT + review_id → ``review_service.reject_review``；
        - ``decide`` REQUEST_CHANGES + review_id →
          ``review_service.request_changes``。
        ReviewService 调用在 inbox UoW 提交后进行（避免嵌套持锁死锁）。

    事件（对应任务目标 6）：
        - ``create`` → ``inbox.item_created``；
        - ``decide`` → ``inbox.item_decided``。
        经 ``SqliteEventPublisher`` 写入 Outbox，与业务写入同事务提交。

    事务边界：
        - ``create``/``decide``/``assign``/``expire``：UoW 内写入 + 事件 commit；
        - ``list_for_actor``/``get``：UoW 内只读（rollback）。
    """

    def __init__(
        self,
        database: Database,
        *,
        repository: SqliteInboxRepository | None = None,
        permission_service: "PermissionService | None" = None,
        review_service: "_ReviewServiceLike | None" = None,
        clock: _SystemClock | None = None,
    ) -> None:
        self._database: Database = database
        self._repository: SqliteInboxRepository = (
            repository or SqliteInboxRepository()
        )
        self._permission_service: "PermissionService" = (
            permission_service or CasbinPermissionService()
        )
        self._review_service: "_ReviewServiceLike | None" = review_service
        self._clock: _SystemClock = clock or _SystemClock()

    # ------------------------------------------------------------------ #
    # create
    # ------------------------------------------------------------------ #

    async def create(
        self,
        request: CreateInboxRequest,
        *,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> InboxItemView:
        """系统创建待办项（不需权限检查）。

        实现顺序：
            1. 校验 actor 与 request 字段；
            2. UoW 内：INSERT 待办项 + ``inbox.item_created`` 事件 + commit；
            3. 返回 ``InboxItemView``。

        :raises ArgumentError: 参数非法。
        """
        actor_ctx, actor_user_id, org_id, trace_id = _actor_context(
            actor_id, actor
        )

        project_id = request.get("project_id")
        title = request.get("title")
        description = request.get("description", "")
        item_type = request.get("item_type")
        artifact_id = request.get("artifact_id")
        review_id = request.get("review_id")
        assigned_to = request.get("assigned_to")
        priority = request.get("priority", "NORMAL")
        metadata = request.get("metadata", {})

        if not isinstance(project_id, str) or not project_id.strip():
            raise ArgumentError("project_id 不能为空")
        if not isinstance(title, str) or not title.strip():
            raise ArgumentError("title 不能为空")
        if not isinstance(description, str):
            raise ArgumentError("description 必须是 str")
        if item_type not in _VALID_ITEM_TYPES:
            raise ArgumentError(
                f"item_type 必须是 REVIEW_REQUEST/CHANGE_REQUEST/"
                f"APPROVAL_REQUEST: {item_type!r}"
            )
        if artifact_id is not None and (
            not isinstance(artifact_id, str) or not artifact_id.strip()
        ):
            raise ArgumentError("artifact_id 必须是非空 str 或 None")
        if review_id is not None and (
            not isinstance(review_id, str) or not review_id.strip()
        ):
            raise ArgumentError("review_id 必须是非空 str 或 None")
        if assigned_to is not None and (
            not isinstance(assigned_to, str) or not assigned_to.strip()
        ):
            raise ArgumentError("assigned_to 必须是非空 str 或 None")
        if priority not in _VALID_PRIORITIES:
            raise ArgumentError(
                f"priority 必须是 LOW/NORMAL/HIGH/URGENT: {priority!r}"
            )
        if not isinstance(metadata, dict):
            raise ArgumentError("metadata 必须是 dict")

        now = self._clock.now()
        iso = _ensure_iso(now)
        item_id = new_inbox_item_id()
        project_id_clean = project_id.strip()
        title_clean = title.strip()
        artifact_id_clean = (
            artifact_id.strip() if isinstance(artifact_id, str) else None
        )
        review_id_clean = (
            review_id.strip() if isinstance(review_id, str) else None
        )
        assigned_to_clean = (
            assigned_to.strip() if isinstance(assigned_to, str) else None
        )

        async with SqliteUnitOfWork(self._database) as uow:
            await self._repository.insert_item(
                uow.connection,
                item_id=item_id,
                project_id=project_id_clean,
                title=title_clean,
                description=description,
                item_type=item_type,
                artifact_id=artifact_id_clean,
                review_id=review_id_clean,
                assigned_to=assigned_to_clean,
                priority=priority,
                status="PENDING",
                created_at=iso,
                created_by=actor_user_id,
                metadata=metadata,
            )
            await self._append_event(
                uow.connection,
                event_type="inbox.item_created",
                aggregate_id=item_id,
                project_id=project_id_clean,
                actor_id=actor_user_id,
                org_id=org_id,
                trace_id=trace_id,
                payload={
                    "item_id": item_id,
                    "project_id": project_id_clean,
                    "item_type": item_type,
                    "title": title_clean,
                    "review_id": review_id_clean,
                    "artifact_id": artifact_id_clean,
                    "assigned_to": assigned_to_clean,
                    "priority": priority,
                    "status": "PENDING",
                    "created_by": actor_user_id,
                },
            )
            await uow.commit()

        rec = await self._load_item(item_id)
        assert rec is not None  # 刚写入，必然存在
        return record_to_view(rec)

    # ------------------------------------------------------------------ #
    # list_for_actor
    # ------------------------------------------------------------------ #

    async def list_for_actor(
        self,
        actor_id: str,
        *,
        status: InboxItemStatus | None = None,
        project_id: str | None = None,
        actor: ActorContext | None = None,
    ) -> list[InboxItemView]:
        """列出当前用户可见的待办项。

        可见性：``assigned_to == actor_id`` 或 ``assigned_to IS NULL``。

        :raises PermissionDeniedError: 无 read inbox 权限。
        :raises ArgumentError: status 非法。
        """
        actor_ctx, actor_user_id, _, _ = _actor_context(actor_id, actor)
        await self._permission_service.require(
            actor_ctx, ACTION_READ, RESOURCE_INBOX
        )

        if status is not None and status not in _VALID_STATUSES:
            raise ArgumentError(
                f"status 必须是 PENDING/DECIDED/EXPIRED: {status!r}"
            )
        if project_id is not None and (
            not isinstance(project_id, str) or not project_id.strip()
        ):
            raise ArgumentError("project_id 必须是非空 str 或 None")

        project_id_clean = (
            project_id.strip()
            if isinstance(project_id, str) and project_id.strip()
            else None
        )

        async with SqliteUnitOfWork(self._database) as uow:
            recs = await self._repository.list_for_actor(
                uow.connection,
                actor_user_id,
                status=status,
                project_id=project_id_clean,
            )
            await uow.rollback()
        return [record_to_view(r) for r in recs]

    # ------------------------------------------------------------------ #
    # get
    # ------------------------------------------------------------------ #

    async def get(
        self,
        item_id: str,
        *,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> InboxItemView:
        """获取待办详情。

        :raises NotFoundError: 待办不存在。
        :raises PermissionDeniedError: 无 read inbox 权限。
        """
        actor_ctx, _, _, _ = _actor_context(actor_id, actor)
        await self._permission_service.require(
            actor_ctx, ACTION_READ, RESOURCE_INBOX
        )

        if not isinstance(item_id, str) or not item_id.strip():
            raise ArgumentError("item_id 不能为空")

        rec = await self._load_item(item_id.strip())
        if rec is None:
            raise NotFoundError(
                "待办项不存在", context={"item_id": item_id}
            )
        return record_to_view(rec)

    # ------------------------------------------------------------------ #
    # decide
    # ------------------------------------------------------------------ #

    async def decide(
        self,
        item_id: str,
        decision: DecideRequest,
        *,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> InboxItemView:
        """人工决策（PENDING → DECIDED）。

        实现顺序：
            1. 校验 actor 与权限（write inbox，APPROVER/ADMIN）；
            2. 校验 decision/comment；
            3. UoW 内：读待办 → 校验状态与 assignee → 乐观锁更新为 DECIDED
               → ``inbox.item_decided`` 事件 → commit；
            4. 提交后如关联 review_id，调用 ReviewService 对应方法（触发后续动作）；
            5. 返回更新后的 ``InboxItemView``。

        :raises PermissionDeniedError: 无 write inbox 权限，或非 assignee/管理员。
        :raises NotFoundError: 待办不存在。
        :raises UnsupportedOperationError: 待办已不在 PENDING 状态。
        :raises VersionConflictError: 乐观锁冲突（并发决策）。
        :raises ArgumentError: 参数非法。
        """
        actor_ctx, actor_user_id, org_id, trace_id = _actor_context(
            actor_id, actor
        )
        await self._permission_service.require(
            actor_ctx, ACTION_WRITE, RESOURCE_INBOX
        )

        if not isinstance(item_id, str) or not item_id.strip():
            raise ArgumentError("item_id 不能为空")

        decision_value = decision.get("decision")
        comment = decision.get("comment")
        meta = decision.get("metadata", {})

        if decision_value not in _VALID_DECISIONS:
            raise ArgumentError(
                f"decision 必须是 APPROVE/REJECT/REQUEST_CHANGES: "
                f"{decision_value!r}"
            )
        if not isinstance(comment, str) or not comment.strip():
            raise ArgumentError("comment 不能为空（人工决策必须说明理由）")
        if not isinstance(meta, dict):
            raise ArgumentError("metadata 必须是 dict")

        item_id_clean = item_id.strip()
        comment_clean = comment.strip()
        now = self._clock.now()
        iso = _ensure_iso(now)

        # ---- UoW 内：决策并写事件 ---- #
        async with SqliteUnitOfWork(self._database) as uow:
            rec = await self._repository.get_item(
                uow.connection, item_id_clean
            )
            if rec is None:
                await uow.rollback()
                raise NotFoundError(
                    "待办项不存在", context={"item_id": item_id_clean}
                )

            # 状态校验：只有 PENDING 可决策
            if rec.status != "PENDING":
                await uow.rollback()
                raise UnsupportedOperationError(
                    f"待办项已处于 {rec.status!r} 状态，不可再决策",
                    context={
                        "item_id": item_id_clean,
                        "current_status": rec.status,
                    },
                )

            # assignee 校验：只有 assignee/管理员可决定
            # （assigned_to 为 None 表示所有 APPROVER 可决定）
            if (
                rec.assigned_to is not None
                and rec.assigned_to != actor_user_id
                and not _is_admin(actor_ctx)
            ):
                await uow.rollback()
                raise PermissionDeniedError(
                    "只有 assignee/管理员可决定该待办",
                    context={
                        "item_id": item_id_clean,
                        "assigned_to": rec.assigned_to,
                        "actor_id": actor_user_id,
                    },
                )

            new_version = await self._repository.update_decision(
                uow.connection,
                item_id_clean,
                decision=decision_value,
                decision_comment=comment_clean,
                decided_by=actor_user_id,
                decided_at=iso,
                expected_version=rec.version_no,
            )
            if new_version == 0:
                await uow.rollback()
                raise VersionConflictError(
                    "待办项版本冲突，可能已被并发决策",
                    context={
                        "item_id": item_id_clean,
                        "expected_version": rec.version_no,
                    },
                    retryable=True,
                )

            await self._append_event(
                uow.connection,
                event_type="inbox.item_decided",
                aggregate_id=item_id_clean,
                project_id=rec.project_id,
                actor_id=actor_user_id,
                org_id=org_id,
                trace_id=trace_id,
                payload={
                    "item_id": item_id_clean,
                    "project_id": rec.project_id,
                    "decision": decision_value,
                    "comment": comment_clean,
                    "decided_by": actor_user_id,
                    "review_id": rec.review_id,
                    "artifact_id": rec.artifact_id,
                    "previous_status": rec.status,
                    "new_status": "DECIDED",
                },
            )
            await uow.commit()

        # ---- 提交后触发 ReviewService（如关联 review_id） ---- #
        await self._trigger_review_action(
            review_id=rec.review_id,
            decision=decision_value,
            comment=comment_clean,
            actor_id=actor_user_id,
            actor=actor_ctx,
        )

        result = await self._load_item(item_id_clean)
        assert result is not None
        return record_to_view(result)

    # ------------------------------------------------------------------ #
    # assign
    # ------------------------------------------------------------------ #

    async def assign(
        self,
        item_id: str,
        user_id: str,
        *,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> InboxItemView:
        """分配待办给指定用户（需 manage inbox 权限，OWNER/ADMIN）。

        :raises PermissionDeniedError: 无 manage inbox 权限。
        :raises NotFoundError: 待办不存在。
        :raises VersionConflictError: 乐观锁冲突。
        :raises ArgumentError: 参数非法。
        """
        actor_ctx, _, _, _ = _actor_context(actor_id, actor)
        await self._permission_service.require(
            actor_ctx, ACTION_MANAGE, RESOURCE_INBOX
        )

        if not isinstance(item_id, str) or not item_id.strip():
            raise ArgumentError("item_id 不能为空")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ArgumentError("user_id 不能为空")

        item_id_clean = item_id.strip()
        user_id_clean = user_id.strip()

        async with SqliteUnitOfWork(self._database) as uow:
            rec = await self._repository.get_item(
                uow.connection, item_id_clean
            )
            if rec is None:
                await uow.rollback()
                raise NotFoundError(
                    "待办项不存在", context={"item_id": item_id_clean}
                )

            new_version = await self._repository.update_assigned_to(
                uow.connection,
                item_id_clean,
                assigned_to=user_id_clean,
                expected_version=rec.version_no,
            )
            if new_version == 0:
                await uow.rollback()
                raise VersionConflictError(
                    "待办项版本冲突",
                    context={
                        "item_id": item_id_clean,
                        "expected_version": rec.version_no,
                    },
                    retryable=True,
                )
            await uow.commit()

        result = await self._load_item(item_id_clean)
        assert result is not None
        return record_to_view(result)

    # ------------------------------------------------------------------ #
    # expire
    # ------------------------------------------------------------------ #

    async def expire(
        self,
        item_id: str,
        *,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> InboxItemView:
        """将待办置为 EXPIRED（系统调用，不需权限检查）。

        :raises NotFoundError: 待办不存在。
        :raises UnsupportedOperationError: 待办已不在 PENDING 状态。
        :raises VersionConflictError: 乐观锁冲突。
        :raises ArgumentError: 参数非法。
        """
        # 系统调用：构造 actor 上下文但不做权限检查
        _actor_context(actor_id, actor)

        if not isinstance(item_id, str) or not item_id.strip():
            raise ArgumentError("item_id 不能为空")

        item_id_clean = item_id.strip()

        async with SqliteUnitOfWork(self._database) as uow:
            rec = await self._repository.get_item(
                uow.connection, item_id_clean
            )
            if rec is None:
                await uow.rollback()
                raise NotFoundError(
                    "待办项不存在", context={"item_id": item_id_clean}
                )

            if rec.status != "PENDING":
                await uow.rollback()
                raise UnsupportedOperationError(
                    f"待办项已处于 {rec.status!r} 状态，不可过期",
                    context={
                        "item_id": item_id_clean,
                        "current_status": rec.status,
                    },
                )

            new_version = await self._repository.update_status_expired(
                uow.connection,
                item_id_clean,
                expected_version=rec.version_no,
            )
            if new_version == 0:
                await uow.rollback()
                raise VersionConflictError(
                    "待办项版本冲突",
                    context={
                        "item_id": item_id_clean,
                        "expected_version": rec.version_no,
                    },
                    retryable=True,
                )
            await uow.commit()

        result = await self._load_item(item_id_clean)
        assert result is not None
        return record_to_view(result)

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    async def _load_item(self, item_id: str) -> InboxItemRecord | None:
        async with SqliteUnitOfWork(self._database) as uow:
            rec = await self._repository.get_item(uow.connection, item_id)
            await uow.rollback()
        return rec

    async def _append_event(
        self,
        conn,
        *,
        event_type: str,
        aggregate_id: str,
        project_id: str,
        actor_id: str,
        org_id: str,
        trace_id: str,
        payload: dict,
    ) -> None:
        """在同一 UoW 事务内向 Outbox 追加 inbox 事件。"""
        publisher = SqliteEventPublisher(conn)
        await publisher.append(
            DomainEvent(
                event_type=event_type,
                aggregate_type="inbox_item",
                aggregate_id=aggregate_id,
                organization_id=org_id,
                project_id=project_id,
                actor=ActorRef(actor_type="USER", actor_id=actor_id),
                trace_id=trace_id,
                payload=payload,
            )
        )

    async def _trigger_review_action(
        self,
        *,
        review_id: str | None,
        decision: str,
        comment: str,
        actor_id: str,
        actor: ActorContext,
    ) -> None:
        """decide 提交后触发 ReviewService 对应方法（如关联 review_id）。

        在 inbox UoW 提交后调用，避免与 ReviewService 自开的 UoW 嵌套持锁
        死锁。若 ``review_service`` 未注入或 ``review_id`` 为空，则无操作。
        ReviewService 内部失败会向上抛出（inbox 决策已落库，由调用方处理）。
        """
        if review_id is None or self._review_service is None:
            return
        if decision == "APPROVE":
            await self._review_service.approve_review(
                review_id,
                actor_id=actor_id,
                comment=comment,
                actor=actor,
            )
        elif decision == "REJECT":
            await self._review_service.reject_review(
                review_id,
                actor_id=actor_id,
                comment=comment,
                actor=actor,
            )
        elif decision == "REQUEST_CHANGES":
            await self._review_service.request_changes(
                review_id,
                actor_id=actor_id,
                comment=comment,
                actor=actor,
            )


# --------------------------------------------------------------------------- #
# 模块级工具：建表
# --------------------------------------------------------------------------- #


async def ensure_inbox_schema(database: Database) -> None:
    """在 ``database`` 上创建 ``inbox_items`` 表（幂等）。

    供测试与开发期首次启动使用；正式部署由 ``migrations/`` 顺序迁移负责。
    """
    from .repository import init_inbox_schema

    async with SqliteUnitOfWork(database) as uow:
        await init_inbox_schema(uow.connection)
        await uow.commit()


__all__ = [
    "ACTION_MANAGE",
    "ACTION_READ",
    "ACTION_WRITE",
    "InboxServiceImpl",
    "PermissionService",
    "RESOURCE_INBOX",
    "ensure_inbox_schema",
]
