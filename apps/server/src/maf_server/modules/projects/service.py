"""Project 应用服务实现。

TASK-033 范围：实现 ``ProjectApplicationService`` 的 9 个方法：
- ``create_project``：ADMIN/DESIGNER 可创建，creator 成为 OWNER。
- ``get_project``：读取项目详情（需为项目成员）。
- ``list_projects``：返回调用者可见项目（成员关系过滤）。
- ``update_project``：OWNER/ADMIN 可更新，乐观锁。
- ``delete_project``：ADMIN 可软删除，乐观锁。
- ``add_member``：OWNER/ADMIN 可添加成员。
- ``remove_member``：OWNER/ADMIN 可移除成员，最后 OWNER 保护。
- ``list_members``：列出项目成员。
- ``update_member_role``：OWNER/ADMIN 可变更角色，最后 OWNER 保护。

权限模型（对应任务文档）：
- create/update/delete_project：``("write", "projects")``
- get/list_project：``("read", "projects")``
- add/remove/update_member：``("manage", "project_members")``

actor_id → ActorContext 构建：service 注入 ``SqliteIamRepository`` 查询用户权限键，
组装 ``ActorContext`` 后调用 ``PermissionService.require``。

事务边界：每个写用例在 ``SqliteUnitOfWork`` 内执行；事件通过 ``SqliteEventPublisher``
与业务写入同事务提交。乐观锁由 ``update_with_expected_version`` 保证。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from datetime import datetime, timezone
from typing import Protocol

from maf_contracts.common import ActorContext
from maf_contracts.events import ActorRef, DomainEvent
from maf_domain.errors import (
    AlreadyExistsError,
    ArgumentError,
    IdempotencyConflictError,
    NotFoundError,
    PermissionDeniedError,
    VersionConflictError,
    ValidationError,
)
from maf_policy import CasbinPermissionService

from maf_server.core.database import Database
from maf_server.core.events import SqliteEventPublisher
from maf_server.core.unit_of_work import SqliteUnitOfWork, update_with_expected_version
from maf_server.modules.iam.repository import SqliteIamRepository

from .repository import (
    MemberRecord,
    ProjectRecord,
    SqliteProjectRepository,
    init_project_extensions_schema,
    member_record_to_view,
    project_record_to_view,
)
from .schemas import (
    ProjectMemberRole,
    ProjectMemberView,
    ProjectPage,
    ProjectStatus,
    ProjectView,
    AddProjectInputRequest,
    ProjectInputView,
    CreateChangeRequest,
    ChangeRequestView,
)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 合法成员角色集合（与迁移脚本 CHECK 约束一致）。
_VALID_MEMBER_ROLES: frozenset[str] = frozenset(
    {"OWNER", "APPROVER", "OBSERVER", "DESIGNER"}
)

#: 合法项目状态集合（与迁移脚本 CHECK 约束一致）。
_VALID_PROJECT_STATUS: frozenset[str] = frozenset({"ACTIVE", "ARCHIVED"})

#: 项目名称最大长度（业务校验，防止滥用）。
_PROJECT_NAME_MAX_LENGTH = 128
_PROJECT_DESC_MAX_LENGTH = 4096


# --------------------------------------------------------------------------- #
# 内部时钟（避免引入额外依赖）
# --------------------------------------------------------------------------- #


class _SystemClock:
    """默认使用系统 UTC 时钟；测试可注入虚拟时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #


def _ensure_iso(dt: datetime) -> str:
    """把 datetime 序列化为 ISO 8601 字符串（带时区）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _validate_project_name(name: str) -> str:
    """校验项目名称：非空、长度限制、去除首尾空白。"""
    if not isinstance(name, str):
        raise ArgumentError("项目名称必须是字符串")
    trimmed = name.strip()
    if not trimmed:
        raise ArgumentError("项目名称不能为空")
    if len(trimmed) > _PROJECT_NAME_MAX_LENGTH:
        raise ArgumentError(
            f"项目名称长度不能超过 {_PROJECT_NAME_MAX_LENGTH} 字符",
            context={"max_length": _PROJECT_NAME_MAX_LENGTH, "actual": len(trimmed)},
        )
    return trimmed


def _validate_project_description(description: str) -> str:
    """校验项目描述：允许空串，长度限制。"""
    if not isinstance(description, str):
        raise ArgumentError("项目描述必须是字符串")
    if len(description) > _PROJECT_DESC_MAX_LENGTH:
        raise ArgumentError(
            f"项目描述长度不能超过 {_PROJECT_DESC_MAX_LENGTH} 字符",
            context={"max_length": _PROJECT_DESC_MAX_LENGTH, "actual": len(description)},
        )
    return description


def _validate_member_role(role: str) -> ProjectMemberRole:
    """校验成员角色合法。"""
    if role not in _VALID_MEMBER_ROLES:
        raise ArgumentError(
            f"非法成员角色: {role!r}，合法角色: {sorted(_VALID_MEMBER_ROLES)}",
            context={"role": role, "valid": sorted(_VALID_MEMBER_ROLES)},
        )
    return role  # type: ignore[return-value]


def _validate_project_status(status: str) -> ProjectStatus:
    """校验项目状态合法。"""
    if status not in _VALID_PROJECT_STATUS:
        raise ArgumentError(
            f"非法项目状态: {status!r}，合法状态: {sorted(_VALID_PROJECT_STATUS)}",
            context={"status": status, "valid": sorted(_VALID_PROJECT_STATUS)},
        )
    return status  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Protocol（保留接口契约，供后续任务扩展）
# --------------------------------------------------------------------------- #


class ProjectApplicationService(Protocol):
    """Project 应用服务接口契约（TASK-033 实现 9 个方法）。"""

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
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
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


# --------------------------------------------------------------------------- #
# 具体实现
# --------------------------------------------------------------------------- #


class ProjectApplicationServiceImpl:
    """``ProjectApplicationService`` 的 SQLite 实现。

    依赖注入：
        - ``database``：``Database``，提供 ``SqliteUnitOfWork`` 事务边界。
        - ``organization_id``：组织 ID，用于 ActorContext 与事件 payload。
        - ``iam_repository``：``SqliteIamRepository``，查询用户权限键。
        - ``project_repository``：``SqliteProjectRepository``，项目与成员 CRUD。
        - ``permission_service``：``CasbinPermissionService``，权限检查。
        - ``clock``：时钟，测试可注入虚拟时钟。
    """

    def __init__(
        self,
        database: Database,
        *,
        organization_id: str,
        iam_repository: SqliteIamRepository | None = None,
        project_repository: SqliteProjectRepository | None = None,
        permission_service: CasbinPermissionService | None = None,
        clock: _SystemClock | None = None,
        artifact_source: object | None = None,
        run_source: object | None = None,
        inbox_service: object | None = None,
    ) -> None:
        self._database: Database = database
        self._organization_id: str = organization_id
        self._iam_repository: SqliteIamRepository = iam_repository or SqliteIamRepository()
        self._project_repository: SqliteProjectRepository = (
            project_repository or SqliteProjectRepository()
        )
        self._permission_service: CasbinPermissionService = (
            permission_service or CasbinPermissionService()
        )
        self._clock: _SystemClock = clock or _SystemClock()
        self._artifact_source = artifact_source
        self._run_source = run_source
        self._inbox_service = inbox_service

    @staticmethod
    async def _call(source: object | None, names: tuple[str, ...], *args: object) -> object | None:
        if source is None:
            return None
        for name in names:
            method = getattr(source, name, None)
            if method is not None:
                value = method(*args)
                return await value if inspect.isawaitable(value) else value
        return None

    # ------------------------------------------------------------------ #
    # 内部：actor_id → ActorContext
    # ------------------------------------------------------------------ #

    async def _build_actor(self, conn, actor_id: str) -> ActorContext:
        """从 ``actor_id`` 查询权限键并构造 ``ActorContext``。

        用户不存在时返回空权限列表（``require`` 会拒绝）。使用传入的 ``conn``
        避免在 UoW 事务内额外开连接。
        """
        if not isinstance(actor_id, str) or not actor_id:
            raise PermissionDeniedError("权限不足：未认证")
        permission_keys = await self._iam_repository.get_user_permissions(
            conn, actor_id
        )
        return ActorContext(
            user_id=actor_id,
            organization_id=self._organization_id,
            permission_keys=permission_keys,
            trace_id="",
        )

    async def _build_actor_read(self, actor_id: str) -> ActorContext:
        """在只读连接中构造 ``ActorContext``（供读用例使用）。"""
        async with self._database.read_connection() as conn:
            return await self._build_actor(conn, actor_id)

    # ------------------------------------------------------------------ #
    # 内部：事件追加
    # ------------------------------------------------------------------ #

    async def _append_event(
        self,
        conn,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        actor_id: str,
        project_id: str | None,
        payload: dict,
    ) -> None:
        """在当前 UoW 事务内向 Outbox 追加领域事件。"""
        publisher = SqliteEventPublisher(conn)
        await publisher.append(
            DomainEvent(
                event_type=event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                organization_id=self._organization_id,
                project_id=project_id,
                actor=ActorRef(actor_type="USER", actor_id=actor_id),
                trace_id="",
                payload=payload,
            )
        )

    # ------------------------------------------------------------------ #
    # 1. create_project
    # ------------------------------------------------------------------ #

    async def create_project(
        self, name: str, description: str, *, actor_id: str
    ) -> ProjectView:
        """创建项目；creator 自动成为 OWNER。

        顺序：校验参数 → 进入 UoW → 构建 actor → 权限检查 ("write", "projects")
        → 生成 UUID4 → 插入 projects 行 → 插入 project_members OWNER 行
        → 追加 ProjectCreated 事件 → commit。
        """
        name = _validate_project_name(name)
        description = _validate_project_description(description)
        now = self._clock.now()
        iso = _ensure_iso(now)
        project_id = str(uuid.uuid4())

        async with SqliteUnitOfWork(self._database) as uow:
            actor = await self._build_actor(uow.connection, actor_id)
            await self._permission_service.require(actor, "write", "projects")

            record = ProjectRecord(
                id=project_id,
                name=name,
                description=description,
                status="ACTIVE",
                created_at=iso,
                created_by=actor_id,
                updated_at=iso,
                version_no=1,
                deleted_at=None,
            )
            await self._project_repository.insert_project(uow.connection, record)

            # creator 成为 OWNER
            member = MemberRecord(
                project_id=project_id,
                user_id=actor_id,
                role="OWNER",
                added_at=iso,
                added_by=actor_id,
                version_no=1,
            )
            await self._project_repository.insert_member(uow.connection, member)

            await self._append_event(
                uow.connection,
                event_type="project.created",
                aggregate_type="project",
                aggregate_id=project_id,
                actor_id=actor_id,
                project_id=project_id,
                payload={
                    "name": name,
                    "description": description,
                    "status": "ACTIVE",
                    "created_by": actor_id,
                    "version": 1,
                },
            )
            await uow.commit()

        return project_record_to_view(record)

    # ------------------------------------------------------------------ #
    # 2. get_project
    # ------------------------------------------------------------------ #

    async def get_project(self, project_id: str, *, actor_id: str) -> ProjectView:
        """读取项目详情；调用者必须是项目成员（否则 404，避免信息泄露）。"""
        if not isinstance(project_id, str) or not project_id:
            raise ArgumentError("项目 ID 不能为空")

        actor = await self._build_actor_read(actor_id)
        await self._permission_service.require(actor, "read", "projects")

        async with self._database.read_connection() as conn:
            record = await self._project_repository.get_project(conn, project_id)
            if record is None:
                raise NotFoundError(
                    f"项目不存在: {project_id}",
                    context={"project_id": project_id},
                )
            # 成员关系检查：非成员返回 404（不泄露存在性）
            member = await self._project_repository.get_member(
                conn, project_id, actor_id
            )
            if member is None:
                raise NotFoundError(
                    f"项目不存在: {project_id}",
                    context={"project_id": project_id},
                )
        return project_record_to_view(record)

    # ------------------------------------------------------------------ #
    # 3. list_projects
    # ------------------------------------------------------------------ #

    async def list_projects(self, *, actor_id: str) -> list[ProjectView]:
        """返回调用者可见项目（成员关系过滤）。"""
        actor = await self._build_actor_read(actor_id)
        await self._permission_service.require(actor, "read", "projects")

        async with self._database.read_connection() as conn:
            records = await self._project_repository.list_projects_by_member(
                conn, actor_id
            )
        return [project_record_to_view(r) for r in records]

    # ------------------------------------------------------------------ #
    # 4. update_project
    # ------------------------------------------------------------------ #

    async def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
        expected_version: int,
        actor_id: str,
    ) -> ProjectView:
        """乐观锁更新项目；OWNER/ADMIN 可操作。

        至少需要提供一个可更新字段；``expected_version`` 必填。
        """
        if not isinstance(project_id, str) or not project_id:
            raise ArgumentError("项目 ID 不能为空")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ArgumentError(
                f"expected_version 必须 >= 1，got {expected_version}",
                context={"expected_version": expected_version},
            )

        # 至少一个可更新字段
        if name is None and description is None and status is None:
            raise ArgumentError("至少需要提供一个可更新字段（name/description/status）")

        # 校验可更新字段
        if name is not None:
            name = _validate_project_name(name)
        if description is not None:
            description = _validate_project_description(description)
        if status is not None:
            status = _validate_project_status(status)

        now = self._clock.now()
        iso = _ensure_iso(now)

        async with SqliteUnitOfWork(self._database) as uow:
            actor = await self._build_actor(uow.connection, actor_id)
            await self._permission_service.require(actor, "write", "projects")

            record = await self._project_repository.get_project(
                uow.connection, project_id
            )
            if record is None:
                raise NotFoundError(
                    f"项目不存在: {project_id}",
                    context={"project_id": project_id},
                )

            # 构造 SET 子句（不含版本列，由 update_with_expected_version 递增）
            assignments: dict[str, object] = {"updated_at": iso}
            changes: dict[str, object] = {}
            if name is not None and name != record.name:
                assignments["name"] = name
                changes["name"] = name
            if description is not None and description != record.description:
                assignments["description"] = description
                changes["description"] = description
            if status is not None and status != record.status:
                assignments["status"] = status
                changes["status"] = status

            if len(assignments) == 1:
                # 只有 updated_at，无实际变更
                await uow.rollback()
                raise ArgumentError("没有实际变更字段（新值与旧值相同）")

            await update_with_expected_version(
                uow.connection,
                "projects",
                assignments=assignments,
                where={"id": project_id},
                expected_version=expected_version,
            )

            await self._append_event(
                uow.connection,
                event_type="project.updated",
                aggregate_type="project",
                aggregate_id=project_id,
                actor_id=actor_id,
                project_id=project_id,
                payload={
                    "changes": changes,
                    "new_version": expected_version + 1,
                    "updated_by": actor_id,
                },
            )
            await uow.commit()

        # 读取更新后的记录返回
        async with self._database.read_connection() as conn:
            updated = await self._project_repository.get_project(conn, project_id)
        assert updated is not None  # 刚提交成功，必定存在
        return project_record_to_view(updated)

    # ------------------------------------------------------------------ #
    # 5. delete_project（软删除）
    # ------------------------------------------------------------------ #

    async def delete_project(
        self, project_id: str, expected_version: int, *, actor_id: str
    ) -> None:
        """ADMIN 软删除项目；设置 ``deleted_at``，乐观锁。"""
        if not isinstance(project_id, str) or not project_id:
            raise ArgumentError("项目 ID 不能为空")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ArgumentError(
                f"expected_version 必须 >= 1，got {expected_version}",
                context={"expected_version": expected_version},
            )

        now = self._clock.now()
        iso = _ensure_iso(now)

        async with SqliteUnitOfWork(self._database) as uow:
            actor = await self._build_actor(uow.connection, actor_id)
            await self._permission_service.require(actor, "write", "projects")

            record = await self._project_repository.get_project(
                uow.connection, project_id
            )
            if record is None:
                raise NotFoundError(
                    f"项目不存在: {project_id}",
                    context={"project_id": project_id},
                )

            await update_with_expected_version(
                uow.connection,
                "projects",
                assignments={
                    "deleted_at": iso,
                    "updated_at": iso,
                },
                where={"id": project_id},
                expected_version=expected_version,
            )

            await self._append_event(
                uow.connection,
                event_type="project.deleted",
                aggregate_type="project",
                aggregate_id=project_id,
                actor_id=actor_id,
                project_id=project_id,
                payload={
                    "deleted_by": actor_id,
                    "deleted_at": iso,
                },
            )
            await uow.commit()

    # ------------------------------------------------------------------ #
    # 6. add_member
    # ------------------------------------------------------------------ #

    async def add_member(
        self,
        project_id: str,
        user_id: str,
        role: str,
        *,
        actor_id: str,
    ) -> ProjectMemberView:
        """OWNER/ADMIN 添加成员；不允许重复添加。"""
        if not isinstance(project_id, str) or not project_id:
            raise ArgumentError("项目 ID 不能为空")
        if not isinstance(user_id, str) or not user_id:
            raise ArgumentError("用户 ID 不能为空")
        role = _validate_member_role(role)

        now = self._clock.now()
        iso = _ensure_iso(now)

        async with SqliteUnitOfWork(self._database) as uow:
            actor = await self._build_actor(uow.connection, actor_id)
            await self._permission_service.require(
                actor, "manage", "project_members"
            )

            record = await self._project_repository.get_project(
                uow.connection, project_id
            )
            if record is None:
                raise NotFoundError(
                    f"项目不存在: {project_id}",
                    context={"project_id": project_id},
                )

            existing = await self._project_repository.get_member(
                uow.connection, project_id, user_id
            )
            if existing is not None:
                raise AlreadyExistsError(
                    f"用户 {user_id} 已是项目 {project_id} 的成员",
                    context={"project_id": project_id, "user_id": user_id},
                )

            member = MemberRecord(
                project_id=project_id,
                user_id=user_id,
                role=role,
                added_at=iso,
                added_by=actor_id,
                version_no=1,
            )
            await self._project_repository.insert_member(uow.connection, member)

            await self._append_event(
                uow.connection,
                event_type="project.member.added",
                aggregate_type="project_member",
                aggregate_id=f"{project_id}:{user_id}",
                actor_id=actor_id,
                project_id=project_id,
                payload={
                    "user_id": user_id,
                    "role": role,
                    "added_by": actor_id,
                },
            )
            await uow.commit()

        return member_record_to_view(member)

    # ------------------------------------------------------------------ #
    # 7. remove_member（最后 OWNER 保护）
    # ------------------------------------------------------------------ #

    async def remove_member(
        self, project_id: str, user_id: str, *, actor_id: str
    ) -> None:
        """OWNER/ADMIN 移除成员；不能移除最后一个 OWNER。"""
        if not isinstance(project_id, str) or not project_id:
            raise ArgumentError("项目 ID 不能为空")
        if not isinstance(user_id, str) or not user_id:
            raise ArgumentError("用户 ID 不能为空")

        async with SqliteUnitOfWork(self._database) as uow:
            actor = await self._build_actor(uow.connection, actor_id)
            await self._permission_service.require(
                actor, "manage", "project_members"
            )

            record = await self._project_repository.get_project(
                uow.connection, project_id
            )
            if record is None:
                raise NotFoundError(
                    f"项目不存在: {project_id}",
                    context={"project_id": project_id},
                )

            existing = await self._project_repository.get_member(
                uow.connection, project_id, user_id
            )
            if existing is None:
                raise NotFoundError(
                    f"成员不存在: 项目 {project_id} 无用户 {user_id}",
                    context={"project_id": project_id, "user_id": user_id},
                )

            # 最后 OWNER 保护：若被移除者是 OWNER 且是最后一个 OWNER，拒绝
            if existing.role == "OWNER":
                owner_count = await self._project_repository.count_members_by_role(
                    uow.connection, project_id, "OWNER"
                )
                if owner_count <= 1:
                    raise ArgumentError(
                        f"不能移除项目 {project_id} 的最后一个 OWNER",
                        context={
                            "project_id": project_id,
                            "user_id": user_id,
                            "owner_count": owner_count,
                        },
                    )

            await self._project_repository.delete_member(
                uow.connection, project_id, user_id
            )

            await self._append_event(
                uow.connection,
                event_type="project.member.removed",
                aggregate_type="project_member",
                aggregate_id=f"{project_id}:{user_id}",
                actor_id=actor_id,
                project_id=project_id,
                payload={
                    "user_id": user_id,
                    "removed_by": actor_id,
                    "previous_role": existing.role,
                },
            )
            await uow.commit()

    # ------------------------------------------------------------------ #
    # 8. list_members
    # ------------------------------------------------------------------ #

    async def list_members(
        self, project_id: str, *, actor_id: str
    ) -> list[ProjectMemberView]:
        """列出项目成员；调用者需为项目成员。"""
        if not isinstance(project_id, str) or not project_id:
            raise ArgumentError("项目 ID 不能为空")

        actor = await self._build_actor_read(actor_id)
        await self._permission_service.require(actor, "read", "projects")

        async with self._database.read_connection() as conn:
            record = await self._project_repository.get_project(conn, project_id)
            if record is None:
                raise NotFoundError(
                    f"项目不存在: {project_id}",
                    context={"project_id": project_id},
                )
            # 成员关系检查：非成员返回 404
            member = await self._project_repository.get_member(
                conn, project_id, actor_id
            )
            if member is None:
                raise NotFoundError(
                    f"项目不存在: {project_id}",
                    context={"project_id": project_id},
                )
            members = await self._project_repository.list_members(conn, project_id)
        return [member_record_to_view(m) for m in members]

    # ------------------------------------------------------------------ #
    # 9. update_member_role（最后 OWNER 保护 + 乐观锁）
    # ------------------------------------------------------------------ #

    async def update_member_role(
        self,
        project_id: str,
        user_id: str,
        new_role: str,
        *,
        actor_id: str,
    ) -> ProjectMemberView:
        """OWNER/ADMIN 变更成员角色；不能让项目失去最后一个 OWNER。"""
        if not isinstance(project_id, str) or not project_id:
            raise ArgumentError("项目 ID 不能为空")
        if not isinstance(user_id, str) or not user_id:
            raise ArgumentError("用户 ID 不能为空")
        new_role = _validate_member_role(new_role)

        async with SqliteUnitOfWork(self._database) as uow:
            actor = await self._build_actor(uow.connection, actor_id)
            await self._permission_service.require(
                actor, "manage", "project_members"
            )

            record = await self._project_repository.get_project(
                uow.connection, project_id
            )
            if record is None:
                raise NotFoundError(
                    f"项目不存在: {project_id}",
                    context={"project_id": project_id},
                )

            existing = await self._project_repository.get_member(
                uow.connection, project_id, user_id
            )
            if existing is None:
                raise NotFoundError(
                    f"成员不存在: 项目 {project_id} 无用户 {user_id}",
                    context={"project_id": project_id, "user_id": user_id},
                )

            old_role = existing.role
            if old_role == new_role:
                await uow.rollback()
                raise ArgumentError(
                    f"成员 {user_id} 角色已是 {new_role}，无需变更",
                    context={
                        "project_id": project_id,
                        "user_id": user_id,
                        "role": new_role,
                    },
                )

            # 最后 OWNER 保护：若把最后一个 OWNER 降级，拒绝
            if old_role == "OWNER" and new_role != "OWNER":
                owner_count = await self._project_repository.count_members_by_role(
                    uow.connection, project_id, "OWNER"
                )
                if owner_count <= 1:
                    raise ArgumentError(
                        f"不能降级项目 {project_id} 的最后一个 OWNER",
                        context={
                            "project_id": project_id,
                            "user_id": user_id,
                            "owner_count": owner_count,
                            "new_role": new_role,
                        },
                    )

            await update_with_expected_version(
                uow.connection,
                "project_members",
                assignments={"role": new_role},
                where={"project_id": project_id, "user_id": user_id},
                expected_version=existing.version_no,
            )

            await self._append_event(
                uow.connection,
                event_type="project.member.role_changed",
                aggregate_type="project_member",
                aggregate_id=f"{project_id}:{user_id}",
                actor_id=actor_id,
                project_id=project_id,
                payload={
                    "user_id": user_id,
                    "old_role": old_role,
                    "new_role": new_role,
                    "changed_by": actor_id,
                    "new_version": existing.version_no + 1,
                },
            )
            await uow.commit()

        # 读取更新后的记录返回
        async with self._database.read_connection() as conn:
            updated = await self._project_repository.get_member(
                conn, project_id, user_id
            )
        assert updated is not None
        return member_record_to_view(updated)

    async def get_project_for_run(self, project_id: str) -> ProjectView | None:
        """Internal read used by RunService; archived rows remain visible for rejection."""
        async with self._database.read_connection() as conn:
            record = await self._project_repository.get_project_include_deleted(conn, project_id)
        return project_record_to_view(record) if record and record.deleted_at is None else None

    async def get_input_version(self, input_id: str) -> ProjectInputView | None:
        async with self._database.read_connection() as conn:
            await init_project_extensions_schema(conn)
            return await self._project_repository.get_input_version(conn, input_id)

    async def add_input_version(
        self, project_id: str, request: AddProjectInputRequest, *, actor_id: str
    ) -> ProjectInputView:
        """Append one immutable project input version with project-scoped idempotency."""
        if not request.get("idempotency_key"):
            raise ArgumentError("idempotency_key 不能为空")
        request_hash = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        artifact = await self._call(
            self._artifact_source, ("get_artifact_version", "get_version", "get"),
            request["upload_artifact_version_id"],
        )
        if not isinstance(artifact, dict):
            raise ValidationError("Artifact 不存在或不可见")
        if artifact.get("project_id") != project_id or artifact.get("status", "COMPLETE") not in {"COMPLETE", "READY", "PUBLISHED"}:
            raise ValidationError("Artifact 必须属于项目且上传完整")
        iso = _ensure_iso(self._clock.now())
        async with SqliteUnitOfWork(self._database) as uow:
            await init_project_extensions_schema(uow.connection)
            actor = await self._build_actor(uow.connection, actor_id)
            await self._permission_service.require(actor, "write", "projects")
            project = await self._project_repository.get_project(uow.connection, project_id)
            if project is None:
                raise NotFoundError(f"项目不存在: {project_id}")
            existing = await self._project_repository.get_input_by_idempotency_key(
                uow.connection, project_id, request["idempotency_key"]
            )
            if existing is not None:
                if existing[1] != request_hash:
                    raise IdempotencyConflictError("相同幂等键对应不同输入")
                await uow.rollback()
                return existing[0]
            item: ProjectInputView = {
                "id": str(uuid.uuid4()), "project_id": project_id,
                "version": await self._project_repository.next_input_version(uow.connection, project_id),
                "name": request["name"].strip(), "content_type": request["content_type"],
                "artifact_version_id": request["upload_artifact_version_id"],
                "change_summary": request["change_summary"], "created_at": iso,
            }
            await self._project_repository.append_input_version(
                uow.connection, item=item, idempotency_key=request["idempotency_key"],
                request_hash=request_hash,
            )
            await self._append_event(
                uow.connection, event_type="project.input_version_added",
                aggregate_type="project_input", aggregate_id=item["id"], actor_id=actor_id,
                project_id=project_id, payload={"version": item["version"], "artifact_version_id": item["artifact_version_id"]},
            )
            await uow.commit()
            return item

    async def create_change_request(
        self, project_id: str, request: CreateChangeRequest, *, actor_id: str
    ) -> ChangeRequestView:
        """Record a request without touching checkpoint/control and create approval inbox work."""
        if not request.get("idempotency_key"):
            raise ArgumentError("idempotency_key 不能为空")
        request_hash = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        run = await self._call(self._run_source, ("get_run",), request["run_id"])
        if not isinstance(run, dict) or run.get("project_id") != project_id:
            raise ValidationError("只能为本项目 Run 创建变更请求")
        if run.get("status") in {"COMPLETED", "FAILED", "CANCELLED"}:
            raise ValidationError("终态 Run 不能创建变更请求")
        iso = _ensure_iso(self._clock.now())
        async with SqliteUnitOfWork(self._database) as uow:
            await init_project_extensions_schema(uow.connection)
            actor = await self._build_actor(uow.connection, actor_id)
            await self._permission_service.require(actor, "write", "projects")
            if await self._project_repository.get_project(uow.connection, project_id) is None:
                raise NotFoundError(f"项目不存在: {project_id}")
            existing = await self._project_repository.get_change_request_by_key(
                uow.connection, project_id, request["idempotency_key"]
            )
            if existing is not None:
                if existing[1] != request_hash:
                    raise IdempotencyConflictError("相同幂等键对应不同变更请求")
                await uow.rollback()
                return existing[0]
            item: ChangeRequestView = {
                "id": str(uuid.uuid4()), "project_id": project_id, "run_id": request["run_id"],
                "status": "PENDING", "title": request["title"].strip(),
                "description": request["description"],
                "affected_requirement_ids": sorted(set(request["affected_requirement_ids"])),
                "requested_action": request["requested_action"], "inbox_item_id": None,
                "created_at": iso,
            }
            await self._project_repository.insert_change_request(
                uow.connection, item=item, idempotency_key=request["idempotency_key"], request_hash=request_hash
            )
            await self._append_event(
                uow.connection, event_type="project.change_requested", aggregate_type="change_request",
                aggregate_id=item["id"], actor_id=actor_id, project_id=project_id,
                payload={"run_id": item["run_id"], "requested_action": item["requested_action"]},
            )
            await uow.commit()
        if self._inbox_service is not None:
            create = getattr(self._inbox_service, "create")
            inbox = create(
                {"project_id": project_id, "title": item["title"], "description": item["description"],
                 "item_type": "CHANGE_REQUEST", "artifact_id": None, "review_id": None,
                 "assigned_to": None, "priority": "HIGH",
                 "metadata": {"change_request_id": item["id"], "run_id": item["run_id"],
                              "requested_action": item["requested_action"]}}, actor_id=actor_id,
            )
            if inspect.isawaitable(inbox):
                inbox = await inbox
            if isinstance(inbox, dict):
                item["inbox_item_id"] = str(inbox.get("id"))
                async with SqliteUnitOfWork(self._database) as uow:
                    await self._project_repository.set_change_request_inbox(
                        uow.connection, item["id"], item["inbox_item_id"]
                    )
                    await uow.commit()
        return item


__all__ = [
    "ProjectApplicationService",
    "ProjectApplicationServiceImpl",
]
