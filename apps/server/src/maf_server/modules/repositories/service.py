"""仓库绑定应用服务实现。

TASK-035 范围：实现 ``RepositoryBindingService`` 的 4 个方法：
- ``bind_repository``：OWNER/ADMIN 可绑定，凭据经 SecretService 存储后只保留 secret_id。
- ``verify_binding``：OWNER/ADMIN 可验证，调用 RepositoryAdapter.verify 并更新状态。
- ``list_bindings``：OWNER/ADMIN/OBSERVER 可列出项目绑定。
- ``remove_binding``：OWNER/ADMIN 可移除绑定。

权限模型（对应 DEFAULT_POLICIES）：
- bind/verify/remove：``("write", "repositories")`` → OWNER、ADMIN
- list：``("read", "repositories")`` → OWNER、ADMIN、OBSERVER

事务边界：每个写用例在 ``SqliteUnitOfWork`` 内执行；事件通过
``SqliteEventPublisher`` 与业务写入同事务提交。``verify_binding`` 中的
Git 操作（adapter.verify）在 UoW 外执行，遵循"写事务不调用 Git/网络"约束。

凭据安全：HTTPS token 经 SecretService.create 存储后返回 secret_id；明文绝不
进入数据库、日志或事件 payload。验证时经 SecretService.resolve(purpose="verify")
短暂取回，传给 adapter 后由 GC 释放。

保留 ``RepositoryApplicationService`` Protocol（TASK-083+ 接口契约）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Protocol

from maf_contracts.common import ActorContext
from maf_contracts.events import ActorRef, DomainEvent
from maf_domain.errors import (
    ArgumentError,
    NotFoundError,
    PermissionDeniedError,
)
from maf_policy import CasbinPermissionService

from maf_server.core.database import Database
from maf_server.core.events import SqliteEventPublisher
from maf_server.core.unit_of_work import SqliteUnitOfWork, update_with_expected_version
from maf_server.gateway.repository.service import RepositoryAdapter, VerifyResult
from maf_server.gateway.secrets.service import SecretService
from maf_server.modules.iam.repository import SqliteIamRepository
from maf_server.modules.projects.repository import SqliteProjectRepository

from .repository import (
    RepositoryBindingRecord,
    SqliteRepositoryBindingRepository,
    binding_record_to_view,
)
from .schemas import (
    CredentialType,
    MergeRepositoryChangeRequest,
    MergeResultView,
    RepositoryChangeView,
    RepositoryHealth,
    VerifyRepositoryRequest,
)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 合法凭据类型集合。
_VALID_CREDENTIAL_TYPES: frozenset[str] = frozenset(
    {"HTTPS_TOKEN", "SSH_KEY", "NONE"}
)

#: 仓库 URL 最大长度（业务校验）。
_REPO_URL_MAX_LENGTH = 2048

#: 分支名最大长度。
_BRANCH_NAME_MAX_LENGTH = 256

#: SecretService resolve purpose（在 LocalSecretService 默认白名单中）。
_VERIFY_PURPOSE: str = "verify"

#: SecretService 中存储 HTTPS token 时的 owner_type。
_SECRET_OWNER_TYPE: str = "repository_binding"


# --------------------------------------------------------------------------- #
# 内部时钟
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


def _validate_repository_url(url: str) -> str:
    """校验仓库 URL：非空、长度限制、去除首尾空白。"""
    if not isinstance(url, str):
        raise ArgumentError("仓库 URL 必须是字符串")
    trimmed = url.strip()
    if not trimmed:
        raise ArgumentError("仓库 URL 不能为空")
    if len(trimmed) > _REPO_URL_MAX_LENGTH:
        raise ArgumentError(
            f"仓库 URL 长度不能超过 {_REPO_URL_MAX_LENGTH} 字符",
            context={"max_length": _REPO_URL_MAX_LENGTH, "actual": len(trimmed)},
        )
    return trimmed


def _validate_branch(branch: str) -> str:
    """校验分支名：非空、长度限制、去除首尾空白。"""
    if not isinstance(branch, str):
        raise ArgumentError("分支名必须是字符串")
    trimmed = branch.strip()
    if not trimmed:
        raise ArgumentError("分支名不能为空")
    if len(trimmed) > _BRANCH_NAME_MAX_LENGTH:
        raise ArgumentError(
            f"分支名长度不能超过 {_BRANCH_NAME_MAX_LENGTH} 字符",
            context={"max_length": _BRANCH_NAME_MAX_LENGTH, "actual": len(trimmed)},
        )
    return trimmed


def _validate_credential_type(cred_type: str) -> CredentialType:
    """校验凭据类型合法。"""
    if cred_type not in _VALID_CREDENTIAL_TYPES:
        raise ArgumentError(
            f"非法凭据类型: {cred_type!r}，合法类型: {sorted(_VALID_CREDENTIAL_TYPES)}",
            context={"credential_type": cred_type, "valid": sorted(_VALID_CREDENTIAL_TYPES)},
        )
    return cred_type  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# TASK-035: RepositoryBindingService Protocol
# --------------------------------------------------------------------------- #


class RepositoryBindingService(Protocol):
    """仓库绑定应用服务接口契约（TASK-035）。"""

    async def bind_repository(
        self,
        project_id: str,
        repository_url: str,
        branch: str,
        *,
        credential_type: str = "NONE",
        credential_plaintext: str | None = None,
        ssh_key_path: str | None = None,
        actor_id: str,
    ) -> dict:
        """绑定仓库到项目；凭据经 SecretService 存储，只保留 secret_id。"""
        ...

    async def verify_binding(self, binding_id: str, *, actor_id: str) -> dict:
        """验证仓库绑定；调用 adapter.verify 并更新 verified 状态。"""
        ...

    async def list_bindings(self, project_id: str, *, actor_id: str) -> list[dict]:
        """列出项目的仓库绑定。"""
        ...

    async def remove_binding(self, binding_id: str, *, actor_id: str) -> None:
        """移除仓库绑定。"""
        ...


# --------------------------------------------------------------------------- #
# TASK-035: RepositoryBindingServiceImpl
# --------------------------------------------------------------------------- #


class RepositoryBindingServiceImpl:
    """``RepositoryBindingService`` 的 SQLite 实现。

    依赖注入：
        - ``database``：``Database``，提供 ``SqliteUnitOfWork`` 事务边界。
        - ``adapter``：``RepositoryAdapter``，执行 Git 验证操作。
        - ``secret_service``：``SecretService``，存储/解析 HTTPS token。
        - ``organization_id``：组织 ID，用于 ActorContext 与事件 payload。
        - ``iam_repository``：``SqliteIamRepository``，查询用户权限键。
        - ``project_repository``：``SqliteProjectRepository``，查询项目与成员。
        - ``binding_repository``：``SqliteRepositoryBindingRepository``，绑定 CRUD。
        - ``permission_service``：``CasbinPermissionService``，权限检查。
        - ``clock``：时钟，测试可注入虚拟时钟。
    """

    def __init__(
        self,
        database: Database,
        *,
        adapter: RepositoryAdapter,
        secret_service: SecretService | None = None,
        organization_id: str = "org-001",
        iam_repository: SqliteIamRepository | None = None,
        project_repository: SqliteProjectRepository | None = None,
        binding_repository: SqliteRepositoryBindingRepository | None = None,
        permission_service: CasbinPermissionService | None = None,
        clock: _SystemClock | None = None,
    ) -> None:
        self._database: Database = database
        self._adapter: RepositoryAdapter = adapter
        self._secret_service: SecretService | None = secret_service
        self._organization_id: str = organization_id
        self._iam_repository: SqliteIamRepository = iam_repository or SqliteIamRepository()
        self._project_repository: SqliteProjectRepository = (
            project_repository or SqliteProjectRepository()
        )
        self._binding_repository: SqliteRepositoryBindingRepository = (
            binding_repository or SqliteRepositoryBindingRepository()
        )
        self._permission_service: CasbinPermissionService = (
            permission_service or CasbinPermissionService()
        )
        self._clock: _SystemClock = clock or _SystemClock()

    # ------------------------------------------------------------------ #
    # 内部：actor_id → ActorContext
    # ------------------------------------------------------------------ #

    async def _build_actor(self, conn, actor_id: str) -> ActorContext:
        """从 ``actor_id`` 查询权限键并构造 ``ActorContext``。"""
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
        """在只读连接中构造 ``ActorContext``。"""
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
        aggregate_id: str,
        actor_id: str,
        project_id: str,
        payload: dict,
    ) -> None:
        """在当前 UoW 事务内向 Outbox 追加领域事件。"""
        publisher = SqliteEventPublisher(conn)
        await publisher.append(
            DomainEvent(
                event_type=event_type,
                aggregate_type="repository_binding",
                aggregate_id=aggregate_id,
                organization_id=self._organization_id,
                project_id=project_id,
                actor=ActorRef(actor_type="USER", actor_id=actor_id),
                trace_id="",
                payload=payload,
            )
        )

    # ------------------------------------------------------------------ #
    # 内部：项目存在性与成员检查
    # ------------------------------------------------------------------ #

    async def _check_project_membership(
        self, conn, project_id: str, actor_id: str
    ) -> None:
        """检查项目存在且 actor 是项目成员；否则抛 NotFoundError。"""
        project = await self._project_repository.get_project(conn, project_id)
        if project is None:
            raise NotFoundError(
                f"项目不存在: {project_id}",
                context={"project_id": project_id},
            )
        member = await self._project_repository.get_member(
            conn, project_id, actor_id
        )
        if member is None:
            # 不泄露项目存在性
            raise NotFoundError(
                f"项目不存在: {project_id}",
                context={"project_id": project_id},
            )

    # ------------------------------------------------------------------ #
    # 1. bind_repository
    # ------------------------------------------------------------------ #

    async def bind_repository(
        self,
        project_id: str,
        repository_url: str,
        branch: str,
        *,
        credential_type: str = "NONE",
        credential_plaintext: str | None = None,
        ssh_key_path: str | None = None,
        actor_id: str,
    ) -> dict:
        """绑定仓库到项目；OWNER/ADMIN 可操作。

        凭据处理：
        - HTTPS_TOKEN：plaintext 经 SecretService.create 存储，只保留 secret_id。
        - SSH_KEY：ssh_key_path 直接存入绑定记录（路径不是密钥）。
        - NONE：不存储凭据（用于本地 file:// 仓库）。
        """
        if not isinstance(project_id, str) or not project_id:
            raise ArgumentError("项目 ID 不能为空")
        repository_url = _validate_repository_url(repository_url)
        branch = _validate_branch(branch)
        credential_type = _validate_credential_type(credential_type)

        # 凭据与类型一致性校验
        if credential_type == "HTTPS_TOKEN" and not credential_plaintext:
            raise ArgumentError("HTTPS_TOKEN 模式需要 credential_plaintext")
        if credential_type == "SSH_KEY" and not ssh_key_path:
            raise ArgumentError("SSH_KEY 模式需要 ssh_key_path")
        if credential_type == "NONE" and (credential_plaintext or ssh_key_path):
            raise ArgumentError("NONE 模式不应提供凭据")

        now = self._clock.now()
        iso = _ensure_iso(now)
        binding_id = str(uuid.uuid4())

        # 存储凭据（在 UoW 外执行，因 SecretService 不涉及数据库事务）
        secret_id: str | None = None
        if credential_type == "HTTPS_TOKEN" and self._secret_service is not None:
            secret_id = await self._secret_service.create(
                owner_type=_SECRET_OWNER_TYPE,
                owner_id=actor_id,
                plaintext=credential_plaintext,  # type: ignore[arg-type]
                name=f"repository_binding/{binding_id}",
            )

        async with SqliteUnitOfWork(self._database) as uow:
            actor = await self._build_actor(uow.connection, actor_id)
            await self._permission_service.require(actor, "write", "repositories")
            await self._check_project_membership(uow.connection, project_id, actor_id)

            record = RepositoryBindingRecord(
                id=binding_id,
                project_id=project_id,
                repository_url=repository_url,
                branch=branch,
                credential_type=credential_type,
                credential_secret_id=secret_id,
                ssh_key_path=ssh_key_path if credential_type == "SSH_KEY" else None,
                verified=False,
                verified_at=None,
                bound_by=actor_id,
                bound_at=iso,
                version_no=1,
            )
            await self._binding_repository.insert_binding(uow.connection, record)

            await self._append_event(
                uow.connection,
                event_type="repository.bound",
                aggregate_id=binding_id,
                actor_id=actor_id,
                project_id=project_id,
                payload={
                    "project_id": project_id,
                    "repository_url": repository_url,
                    "branch": branch,
                    "credential_type": credential_type,
                    "bound_by": actor_id,
                    "bound_at": iso,
                },
            )
            await uow.commit()

        return binding_record_to_view(record)

    # ------------------------------------------------------------------ #
    # 2. verify_binding
    # ------------------------------------------------------------------ #

    async def verify_binding(self, binding_id: str, *, actor_id: str) -> dict:
        """验证仓库绑定；OWNER/ADMIN 可操作。

        流程：
        1. 读取绑定记录（UoW 内）。
        2. 权限检查 + 项目成员检查。
        3. 在 UoW 外解析凭据并调用 adapter.verify（Git 操作不在事务内）。
        4. 在新 UoW 内更新 verified/verified_at 并追加事件。
        """
        if not isinstance(binding_id, str) or not binding_id:
            raise ArgumentError("绑定 ID 不能为空")

        # 1. 读取绑定记录并检查权限
        async with SqliteUnitOfWork(self._database) as uow:
            actor = await self._build_actor(uow.connection, actor_id)
            await self._permission_service.require(actor, "write", "repositories")

            record = await self._binding_repository.get_binding(
                uow.connection, binding_id
            )
            if record is None:
                raise NotFoundError(
                    f"仓库绑定不存在: {binding_id}",
                    context={"binding_id": binding_id},
                )
            await self._check_project_membership(
                uow.connection, record.project_id, actor_id
            )
            # 读取完毕，回滚此 UoW（不在此事务内做 Git 操作）
            await uow.rollback()

        # 2. 解析凭据（在 UoW 外；明文短暂存在，用完由 GC 释放）
        credentials = await self._resolve_credentials(record)

        # 3. 调用 adapter.verify（Git 操作，不在事务内）
        result: VerifyResult = await self._adapter.verify(
            record.repository_url,
            credentials,
            expected_branch=record.branch,
        )

        # 4. 更新绑定状态（新 UoW）
        now = self._clock.now()
        iso = _ensure_iso(now)
        async with SqliteUnitOfWork(self._database) as uow:
            await self._permission_service.require(
                await self._build_actor(uow.connection, actor_id),
                "write",
                "repositories",
            )

            await update_with_expected_version(
                uow.connection,
                "project_repositories",
                assignments={
                    "verified": 1 if result.verified else 0,
                    "verified_at": iso,
                },
                where={"id": binding_id},
                expected_version=record.version_no,
            )

            await self._append_event(
                uow.connection,
                event_type="repository.verified",
                aggregate_id=binding_id,
                actor_id=actor_id,
                project_id=record.project_id,
                payload={
                    "verified": result.verified,
                    "verified_at": iso,
                    "repository_url": record.repository_url,
                    "branch": record.branch,
                    "error": result.error,
                    "can_read": result.repository_info.get("can_read") if result.repository_info else None,
                    "can_write": result.repository_info.get("can_write") if result.repository_info else None,
                    "default_branch": result.repository_info.get("default_branch") if result.repository_info else None,
                },
            )
            await uow.commit()

        # 读取更新后的记录返回
        async with self._database.read_connection() as conn:
            updated = await self._binding_repository.get_binding(conn, binding_id)
        assert updated is not None
        return binding_record_to_view(updated)

    # ------------------------------------------------------------------ #
    # 3. list_bindings
    # ------------------------------------------------------------------ #

    async def list_bindings(self, project_id: str, *, actor_id: str) -> list[dict]:
        """列出项目的仓库绑定；OWNER/ADMIN/OBSERVER 可操作。"""
        if not isinstance(project_id, str) or not project_id:
            raise ArgumentError("项目 ID 不能为空")

        actor = await self._build_actor_read(actor_id)
        await self._permission_service.require(actor, "read", "repositories")

        async with self._database.read_connection() as conn:
            await self._check_project_membership(conn, project_id, actor_id)
            records = await self._binding_repository.list_by_project(
                conn, project_id
            )
        return [binding_record_to_view(r) for r in records]

    # ------------------------------------------------------------------ #
    # 4. remove_binding
    # ------------------------------------------------------------------ #

    async def remove_binding(self, binding_id: str, *, actor_id: str) -> None:
        """移除仓库绑定；OWNER/ADMIN 可操作。"""
        if not isinstance(binding_id, str) or not binding_id:
            raise ArgumentError("绑定 ID 不能为空")

        async with SqliteUnitOfWork(self._database) as uow:
            actor = await self._build_actor(uow.connection, actor_id)
            await self._permission_service.require(actor, "write", "repositories")

            record = await self._binding_repository.get_binding(
                uow.connection, binding_id
            )
            if record is None:
                raise NotFoundError(
                    f"仓库绑定不存在: {binding_id}",
                    context={"binding_id": binding_id},
                )
            await self._check_project_membership(
                uow.connection, record.project_id, actor_id
            )

            await self._binding_repository.delete_binding(
                uow.connection, binding_id
            )

            await self._append_event(
                uow.connection,
                event_type="repository.removed",
                aggregate_id=binding_id,
                actor_id=actor_id,
                project_id=record.project_id,
                payload={
                    "project_id": record.project_id,
                    "repository_url": record.repository_url,
                    "branch": record.branch,
                    "removed_by": actor_id,
                },
            )
            await uow.commit()

    # ------------------------------------------------------------------ #
    # 内部：凭据解析
    # ------------------------------------------------------------------ #

    async def _resolve_credentials(
        self, record: RepositoryBindingRecord
    ) -> dict:
        """从绑定记录解析凭据 dict（供 adapter 使用）。

        - HTTPS_TOKEN：经 SecretService.resolve 取回 token 明文。
        - SSH_KEY：直接返回路径（路径不是密钥）。
        - NONE：返回空凭据。
        """
        if record.credential_type == "HTTPS_TOKEN":
            if record.credential_secret_id and self._secret_service is not None:
                # 使用 bound_by 作为 actor_id（secret 的 owner）
                token = await self._secret_service.resolve(
                    record.credential_secret_id,
                    purpose=_VERIFY_PURPOSE,
                    actor_id=record.bound_by,
                )
                return {"type": "HTTPS_TOKEN", "token": token}
            return {"type": "NONE"}
        elif record.credential_type == "SSH_KEY":
            if record.ssh_key_path:
                return {"type": "SSH_KEY", "ssh_key_path": record.ssh_key_path}
            return {"type": "NONE"}
        return {"type": "NONE"}


# --------------------------------------------------------------------------- #
# TASK-083+ 占位 Protocol（保留，本任务不修改）
# --------------------------------------------------------------------------- #


class RepositoryApplicationService(Protocol):
    async def verify_binding(
        self, actor: ActorContext, binding_id: str, request: VerifyRepositoryRequest
    ) -> RepositoryHealth:
        """验证 GitHub 或 Local Git 绑定。

        读取 Secret 引用并调用 RepositoryAdapter；检查仓库存在、base branch 可解析以及所需
        能力。只执行无破坏读取/临时探测，清理测试分支。保存脱敏状态和 base commit。
        """
        ...

    async def get_run_change(self, actor: ActorContext, run_id: str) -> RepositoryChangeView:
        """返回 Run 的集成分支、head、PR、checks 和 merge 投影；先检查项目权限。"""
        ...

    async def merge_change(
        self, actor: ActorContext, change_id: str, request: MergeRepositoryChangeRequest
    ) -> MergeResultView:
        """执行最终受控合并。

        顺序：验证操作者权限；确认 Final Inbox Decision 为 APPROVE 且主题版本匹配；确认产品
        验收、代码评审、测试和仓库 checks 全部通过；重新读取远端 head 并与
        expected_head_commit 比较；使用幂等 RepositoryCommand 调 Gateway；保存结果与事件。
        任一条件变化都返回冲突，不绕过分支保护。
        """
        ...


__all__ = [
    "RepositoryBindingService",
    "RepositoryBindingServiceImpl",
    "RepositoryApplicationService",
]
