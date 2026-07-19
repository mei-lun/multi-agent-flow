"""模型连接配置管理应用用例接口与实现。

TASK-037 范围：
- 定义 ``ModelConnectionService`` Protocol（配置接口）与 ``ModelConnectionServiceImpl``
  具体实现，覆盖 ``create_connection``/``get_connection``/``list_connections``/
  ``update_connection``/``delete_connection``/``test_connection``。
- 本任务**只做连接配置管理，不执行推理调用**；``test_connection`` 只验证凭据可解析
  与 URL 格式正确。

安全约束（对应 TASK-037 验收）：
- 凭据明文经 ``SecretService`` 存储，数据库只保存 ``secret_id`` 与不可逆指纹；
- ``ModelConnectionView`` 只返回 ``credential_type`` 与 ``credential_fingerprint``，
  绝不返回明文或 ``secret_id``；
- 凭据轮换采用"先建新 secret、提交后删旧 secret"策略（与 TASK-032 一致），
  UoW 失败时 best-effort 删除新 secret，避免孤儿引用；
- 事件 payload 不含凭据明文。

权限（对应任务说明第 4 条）：
- create/update/delete：``require(actor, "write", "model_connections")`` → ADMIN/DESIGNER；
- get/list/test：``require(actor, "read", "model_connections")`` → ADMIN/DESIGNER/OBSERVER。

事务边界：每个写用例在 ``SqliteUnitOfWork`` 内执行；Outbox 事件与业务修改同事务提交。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urlparse

from maf_contracts.common import ActorContext
from maf_contracts.events import ActorRef, DomainEvent
from maf_domain.errors import (
    ArgumentError,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedOperationError,
    VersionConflictError,
)
from maf_policy import CasbinPermissionService

from maf_server.core.clock import Clock
from maf_server.core.database import Database
from maf_server.core.events import SqliteEventPublisher
from maf_server.core.unit_of_work import SqliteUnitOfWork, update_with_expected_version
from maf_server.gateway.model.adapters import ProviderAdapterFactory
from maf_server.gateway.model.probe import (
    ModelProbeService,
    VerificationResult,
)
from maf_server.gateway.secrets.service import SecretService

from .repository import (
    ModelConnectionRecord,
    SqliteModelConnectionRepository,
    init_schema,
    new_connection_id,
)
from .schemas import (
    ALLOWED_CREDENTIAL_TYPES,
    ALLOWED_PROVIDERS,
    STATUS_ERROR,
    STATUS_UNVERIFIED,
    STATUS_VERIFIED,
    CreateModelConnectionRequest,
    ModelConnectionView,
    TestResult,
    UpdateModelConnectionRequest,
)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 资源与动作标识（与 ``maf_policy.DEFAULT_POLICIES`` 对齐）。
_RESOURCE_MODEL_CONNECTIONS: str = "model_connections"
_ACTION_READ: str = "read"
_ACTION_WRITE: str = "write"

#: 凭据 secret 在 SecretService 中的 owner_type。
_SECRET_OWNER_TYPE: str = "model_connection"

#: ``test_connection`` 解析凭据使用的 purpose（在 LocalSecretService 默认白名单内）。
_VERIFY_PURPOSE: str = "verify"

#: 事件类型与聚合类型。
_EVENT_CREATED: str = "model_connection.created"
_EVENT_UPDATED: str = "model_connection.updated"
_EVENT_DELETED: str = "model_connection.deleted"
_AGGREGATE_TYPE: str = "model_connection"


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


class _SystemClock:
    """默认使用系统 UTC 时钟；测试可注入虚拟时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def wait_until(self, deadline: datetime) -> None:
        return None


class PermissionService(Protocol):
    """权限服务协议（与 ``iam.service.PermissionService`` 结构一致）。

    本模块为保持自包含而本地声明此 Protocol；``CasbinPermissionService`` 满足该协议。
    """

    async def require(self, actor: ActorContext, action: str, resource: str) -> None:
        """无返回表示允许；无匹配授权或上下文缺失必须拒绝。"""
        ...


def _fingerprint(plaintext: str) -> str:
    """不可逆指纹：``sha256(plaintext)[:8] + ".." + plaintext[-4:]``。

    与 TASK-029 ``LocalSecretService._fingerprint`` 和 TASK-032
    ``IamServiceImpl._fingerprint`` 实现一致，用于运维识别与脱敏展示。
    指纹后 4 位为明文末尾（设计文档 §25.1 允许），不构成前缀泄露。
    """
    digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    tail = plaintext[-4:] if len(plaintext) >= 4 else plaintext
    return f"{digest[:8]}..{tail}"


def _ensure_iso(value: datetime) -> str:
    """把 datetime 序列化为带时区 ISO 8601 字符串；naive 视为 UTC。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _require_actor(actor: ActorContext) -> tuple[str, str, str]:
    """校验 actor 并返回 (user_id, organization_id, trace_id)。

    未认证抛 ``PermissionDeniedError``（经 PermissionService 之外的前置校验）。
    组织 ID 缺失时回退为 ``"system"``，供 SecretService owner 与事件 organization_id 使用。
    """
    if not isinstance(actor, dict):
        raise PermissionDeniedError("权限不足：调用者上下文缺失")
    user_id = actor.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        raise PermissionDeniedError("权限不足：未认证")
    organization_id = actor.get("organization_id")
    if not isinstance(organization_id, str) or not organization_id:
        organization_id = "system"
    trace_id = actor.get("trace_id")
    if not isinstance(trace_id, str):
        trace_id = ""
    return user_id, organization_id, trace_id


def _record_to_view(record: ModelConnectionRecord) -> ModelConnectionView:
    """把 ``ModelConnectionRecord`` 映射为对外视图，不含 ``secret_id`` 与明文。"""
    return ModelConnectionView(
        id=record.id,
        name=record.name,
        provider=record.provider,
        model_id=record.model_id,
        api_base=record.api_base,
        credential_type=record.credential_type,
        credential_fingerprint=record.credential_fingerprint,
        status=record.status,
        created_by=record.created_by,
        created_at=record.created_at,
        updated_at=record.updated_at,
        version=record.version_no,
    )


def _is_valid_url(url: str) -> bool:
    """校验 ``api_base`` 是否为合法的 http/https URL（含 host 段）。

    ``local`` 供应商（如 Ollama）通常使用 ``http://localhost:port``，允许 http。
    """
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


# --------------------------------------------------------------------------- #
# Protocol（配置接口）
# --------------------------------------------------------------------------- #


class ModelConnectionService(Protocol):
    """模型连接配置管理协议（TASK-037）。

    所有方法接受 ``ActorContext``（含 ``user_id``/``permission_keys`` 等），
    由 ``PermissionService.require`` 校验 RBAC 权限。凭据明文绝不进入返回值或事件。
    """

    async def create_connection(
        self,
        name: str,
        provider: str,
        model_id: str,
        api_base: str,
        credential_type: str,
        credential_value: str,
        *,
        actor: ActorContext,
    ) -> ModelConnectionView:
        """创建模型连接（仅 ADMIN/DESIGNER）；凭据经 SecretService 存储。"""
        ...

    async def get_connection(
        self, connection_id: str, *, actor: ActorContext
    ) -> ModelConnectionView:
        """获取连接详情（不含凭据明文与 secret_id）。"""
        ...

    async def list_connections(
        self, *, actor: ActorContext
    ) -> list[ModelConnectionView]:
        """列出所有连接。"""
        ...

    async def update_connection(
        self,
        connection_id: str,
        *,
        name: str | None = None,
        api_base: str | None = None,
        credential_value: str | None = None,
        expected_version: int,
        actor: ActorContext,
    ) -> ModelConnectionView:
        """更新连接（仅 ADMIN/DESIGNER）；更新凭据则经 SecretService 轮换。"""
        ...

    async def delete_connection(
        self,
        connection_id: str,
        expected_version: int,
        *,
        actor: ActorContext,
    ) -> None:
        """删除连接（仅 ADMIN/DESIGNER），同时删除凭据。"""
        ...

    async def test_connection(
        self, connection_id: str, *, actor: ActorContext
    ) -> TestResult:
        """测试连接配置完整性（凭据可解析 + URL 格式正确），不含推理调用。"""
        ...

    async def verify_connection(
        self, connection_id: str, *, actor: ActorContext
    ) -> "VerificationResult":
        """分层验证模型连接（config→credential→network→model）。

        TASK-039 范围：在 ``test_connection``（仅配置层）之上叠加凭据格式、
        网络可达性、模型存在性三层；任一层失败时后继层标 SKIP。
        凭据明文经 ``SecretService.resolve`` 短暂存在于本调用栈，绝不进入
        返回值或事件 payload。

        :returns: ``VerificationResult``，含 4 层结果与 ``overall_passed``。
        """
        ...


# --------------------------------------------------------------------------- #
# 具体实现
# --------------------------------------------------------------------------- #


class ModelConnectionServiceImpl:
    """``ModelConnectionService`` 的本地 SQLite 实现。

    依赖注入：
        - ``database``：``Database``，提供 ``SqliteUnitOfWork`` 事务边界；
        - ``repository``：``SqliteModelConnectionRepository``，model_connections 表 CRUD；
        - ``secret_service``：``SecretService``，凭据明文存储后端；
        - ``permission_service``：``PermissionService``，RBAC 权限校验；
        - ``clock``：``Clock``，可注入虚拟时钟用于测试。

    安全约束：
        - 明文凭据绝不进入实例属性、日志、异常 context 或事件 payload；
        - ``secret_id`` 绝不进入 ``ModelConnectionView``；
        - 凭据轮换采用"先建新、提交后删旧"策略，失败时 best-effort 清理孤儿 secret。
    """

    def __init__(
        self,
        database: Database,
        *,
        secret_service: SecretService | None = None,
        repository: SqliteModelConnectionRepository | None = None,
        permission_service: PermissionService | None = None,
        clock: Clock | None = None,
        adapter_factory: ProviderAdapterFactory | None = None,
        probe_service: ModelProbeService | None = None,
    ) -> None:
        self._database: Database = database
        self._repository: SqliteModelConnectionRepository = (
            repository or SqliteModelConnectionRepository()
        )
        self._secret_service: SecretService | None = secret_service
        self._permission_service: PermissionService = (
            permission_service or CasbinPermissionService()
        )
        self._clock: Clock = clock or _SystemClock()
        # TASK-039：分层验证所需依赖，惰性构造以避免无 SecretService 时报错。
        self._adapter_factory: ProviderAdapterFactory | None = adapter_factory
        self._probe_service: ModelProbeService | None = probe_service

    # ------------------------------------------------------------------ #
    # create_connection
    # ------------------------------------------------------------------ #

    async def create_connection(
        self,
        name: str,
        provider: str,
        model_id: str,
        api_base: str,
        credential_type: str,
        credential_value: str,
        *,
        actor: ActorContext,
    ) -> ModelConnectionView:
        """创建模型连接；仅 ADMIN/DESIGNER。

        实现顺序（对应《接口设计与实现规范》第 6 节）：
        1. 校验 actor、权限（``write`` ``model_connections``）；
        2. 校验 name/provider/model_id/api_base/credential_type/credential_value；
        3. 生成 connection_id，本地计算指纹；
        4. UoW 之外调用 ``secret_service.create`` 存储凭据明文（短事务原则）；
        5. UoW 内：INSERT model_connections（status=UNVERIFIED, version=1），
           追加 ``model_connection.created`` Outbox 事件，commit；
        6. UoW 失败时 best-effort 删除新 secret，避免孤儿引用；
        7. 返回不含明文与 secret_id 的 ``ModelConnectionView``。

        :raises ArgumentError: 参数缺失或取值非法。
        :raises PermissionDeniedError: 非 ADMIN/DESIGNER。
        :raises UnsupportedOperationError: 未注入 SecretService。
        """
        actor_id, org_id, trace_id = _require_actor(actor)
        await self._permission_service.require(
            actor, _ACTION_WRITE, _RESOURCE_MODEL_CONNECTIONS
        )

        name = self._validate_name(name)
        provider = self._validate_provider(provider)
        model_id = self._validate_required(model_id, "model_id")
        api_base = self._validate_required(api_base, "api_base")
        credential_type = self._validate_credential_type(credential_type)
        if not isinstance(credential_value, str) or not credential_value:
            raise ArgumentError("credential_value 不能为空")

        if self._secret_service is None:
            raise UnsupportedOperationError(
                "创建模型连接需要 SecretService，但未注入",
            )

        connection_id = new_connection_id()
        fingerprint = _fingerprint(credential_value)
        now = self._clock.now()
        iso = _ensure_iso(now)

        # 短事务：明文经 SecretService 存储，返回 opaque secret_id。
        secret_id = await self._secret_service.create(
            _SECRET_OWNER_TYPE, connection_id, credential_value
        )

        try:
            async with SqliteUnitOfWork(self._database) as uow:
                await self._repository.insert(
                    uow.connection,
                    connection_id=connection_id,
                    name=name,
                    provider=provider,
                    model_id=model_id,
                    api_base=api_base,
                    credential_type=credential_type,
                    credential_secret_id=secret_id,
                    credential_fingerprint=fingerprint,
                    created_by=actor_id,
                    created_at=iso,
                )
                await self._append_event(
                    uow.connection,
                    event_type=_EVENT_CREATED,
                    connection_id=connection_id,
                    actor_id=actor_id,
                    org_id=org_id,
                    trace_id=trace_id,
                    payload={
                        "connection_id": connection_id,
                        "name": name,
                        "provider": provider,
                        "model_id": model_id,
                        "version": 1,
                    },
                )
                await uow.commit()
        except BaseException:
            # UoW 失败：新 secret 已无引用，best-effort 删除，不阻塞原异常。
            await self._safe_delete_secret(secret_id)
            raise

        return ModelConnectionView(
            id=connection_id,
            name=name,
            provider=provider,
            model_id=model_id,
            api_base=api_base,
            credential_type=credential_type,
            credential_fingerprint=fingerprint,
            status=STATUS_UNVERIFIED,
            created_by=actor_id,
            created_at=iso,
            updated_at=iso,
            version=1,
        )

    # ------------------------------------------------------------------ #
    # get_connection
    # ------------------------------------------------------------------ #

    async def get_connection(
        self, connection_id: str, *, actor: ActorContext
    ) -> ModelConnectionView:
        """获取连接详情；仅返回 fingerprint，不含明文与 secret_id。"""
        _require_actor(actor)
        await self._permission_service.require(
            actor, _ACTION_READ, _RESOURCE_MODEL_CONNECTIONS
        )

        if not isinstance(connection_id, str) or not connection_id:
            raise ArgumentError("connection_id 不能为空")

        async with SqliteUnitOfWork(self._database) as uow:
            record = await self._repository.get_by_id(
                uow.connection, connection_id
            )
            await uow.rollback()

        if record is None:
            raise NotFoundError(
                "模型连接不存在",
                context={"connection_id": connection_id},
            )
        return _record_to_view(record)

    # ------------------------------------------------------------------ #
    # list_connections
    # ------------------------------------------------------------------ #

    async def list_connections(
        self, *, actor: ActorContext
    ) -> list[ModelConnectionView]:
        """列出所有连接，按创建时间升序。"""
        _require_actor(actor)
        await self._permission_service.require(
            actor, _ACTION_READ, _RESOURCE_MODEL_CONNECTIONS
        )

        async with SqliteUnitOfWork(self._database) as uow:
            records = await self._repository.list_all(uow.connection)
            await uow.rollback()

        return [_record_to_view(r) for r in records]

    # ------------------------------------------------------------------ #
    # update_connection
    # ------------------------------------------------------------------ #

    async def update_connection(
        self,
        connection_id: str,
        *,
        name: str | None = None,
        api_base: str | None = None,
        credential_value: str | None = None,
        expected_version: int,
        actor: ActorContext,
    ) -> ModelConnectionView:
        """更新连接；仅 ADMIN/DESIGNER。更新凭据则经 SecretService 轮换。

        - 至少提供 ``name``/``api_base``/``credential_value`` 之一；
        - ``expected_version`` 必须为 int，不匹配抛 ``VersionConflictError``；
        - 更新凭据采用"先建新 secret、提交后删旧 secret"策略（与 TASK-032 一致）。
        """
        actor_id, org_id, trace_id = _require_actor(actor)
        await self._permission_service.require(
            actor, _ACTION_WRITE, _RESOURCE_MODEL_CONNECTIONS
        )

        if not isinstance(connection_id, str) or not connection_id:
            raise ArgumentError("connection_id 不能为空")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise ArgumentError("expected_version 必须为 int")

        if name is None and api_base is None and credential_value is None:
            raise ArgumentError("至少需要更新 name/api_base/credential_value 之一")

        if name is not None:
            name = self._validate_name(name)
        if api_base is not None:
            api_base = self._validate_required(api_base, "api_base")
        if credential_value is not None and (
            not isinstance(credential_value, str) or not credential_value
        ):
            raise ArgumentError("credential_value 不能为空")

        now = self._clock.now()
        iso = _ensure_iso(now)

        if credential_value is not None:
            return await self._update_with_credential(
                connection_id=connection_id,
                name=name,
                api_base=api_base,
                credential_value=credential_value,
                expected_version=expected_version,
                actor_id=actor_id,
                org_id=org_id,
                trace_id=trace_id,
                iso=iso,
            )
        return await self._update_without_credential(
            connection_id=connection_id,
            name=name,
            api_base=api_base,
            expected_version=expected_version,
            actor_id=actor_id,
            org_id=org_id,
            trace_id=trace_id,
            iso=iso,
        )

    async def _update_with_credential(
        self,
        *,
        connection_id: str,
        name: str | None,
        api_base: str | None,
        credential_value: str,
        expected_version: int,
        actor_id: str,
        org_id: str,
        trace_id: str,
        iso: str,
    ) -> ModelConnectionView:
        """凭据轮换路径：先建新 secret，UoW 内更新引用，提交后删旧 secret。"""
        if self._secret_service is None:
            raise UnsupportedOperationError(
                "更新凭据需要 SecretService，但未注入",
            )

        new_fingerprint = _fingerprint(credential_value)
        new_secret_id = await self._secret_service.create(
            _SECRET_OWNER_TYPE, connection_id, credential_value
        )
        old_secret_id: str | None = None
        new_version = expected_version + 1

        try:
            async with SqliteUnitOfWork(self._database) as uow:
                existing = await self._repository.get_by_id(
                    uow.connection, connection_id
                )
                if existing is None:
                    await uow.rollback()
                    raise NotFoundError(
                        "模型连接不存在",
                        context={"connection_id": connection_id},
                    )
                old_secret_id = existing.credential_secret_id

                assignments: dict[str, object] = {
                    "credential_secret_id": new_secret_id,
                    "credential_fingerprint": new_fingerprint,
                    "updated_at": iso,
                }
                if name is not None:
                    assignments["name"] = name
                if api_base is not None:
                    assignments["api_base"] = api_base

                await update_with_expected_version(
                    uow.connection,
                    "model_connections",
                    assignments=assignments,
                    where={"id": connection_id},
                    expected_version=expected_version,
                )
                await self._append_event(
                    uow.connection,
                    event_type=_EVENT_UPDATED,
                    connection_id=connection_id,
                    actor_id=actor_id,
                    org_id=org_id,
                    trace_id=trace_id,
                    payload={
                        "connection_id": connection_id,
                        "changed": self._changed_fields(
                            name, api_base, credential_value
                        ),
                        "credential_rotated": True,
                        "version": new_version,
                    },
                )
                await uow.commit()
        except BaseException:
            # UoW 失败：新 secret 已无引用，best-effort 删除，不阻塞原异常。
            await self._safe_delete_secret(new_secret_id)
            raise

        # commit 成功：旧 secret 已无引用，best-effort 删除。
        if old_secret_id is not None:
            await self._safe_delete_secret(old_secret_id)

        return await self._reload_view(connection_id, expected_version=expected_version)

    async def _update_without_credential(
        self,
        *,
        connection_id: str,
        name: str | None,
        api_base: str | None,
        expected_version: int,
        actor_id: str,
        org_id: str,
        trace_id: str,
        iso: str,
    ) -> ModelConnectionView:
        """非凭据更新路径：仅更新 name/api_base，乐观锁递增版本号。"""
        new_version = expected_version + 1
        assignments: dict[str, object] = {"updated_at": iso}
        if name is not None:
            assignments["name"] = name
        if api_base is not None:
            assignments["api_base"] = api_base

        async with SqliteUnitOfWork(self._database) as uow:
            existing = await self._repository.get_by_id(
                uow.connection, connection_id
            )
            if existing is None:
                await uow.rollback()
                raise NotFoundError(
                    "模型连接不存在",
                    context={"connection_id": connection_id},
                )
            await update_with_expected_version(
                uow.connection,
                "model_connections",
                assignments=assignments,
                where={"id": connection_id},
                expected_version=expected_version,
            )
            await self._append_event(
                uow.connection,
                event_type=_EVENT_UPDATED,
                connection_id=connection_id,
                actor_id=actor_id,
                org_id=org_id,
                trace_id=trace_id,
                payload={
                    "connection_id": connection_id,
                    "changed": self._changed_fields(name, api_base, None),
                    "credential_rotated": False,
                    "version": new_version,
                },
            )
            await uow.commit()

        return await self._reload_view(connection_id, expected_version=expected_version)

    # ------------------------------------------------------------------ #
    # delete_connection
    # ------------------------------------------------------------------ #

    async def delete_connection(
        self,
        connection_id: str,
        expected_version: int,
        *,
        actor: ActorContext,
    ) -> None:
        """删除连接；仅 ADMIN/DESIGNER。同时 best-effort 删除凭据。

        - ``expected_version`` 不匹配抛 ``VersionConflictError``；
        - 连接不存在抛 ``NotFoundError``；
        - 先在 UoW 内删除 DB 行（乐观锁）并追加事件，提交后 best-effort 删除 secret。
        """
        actor_id, org_id, trace_id = _require_actor(actor)
        await self._permission_service.require(
            actor, _ACTION_WRITE, _RESOURCE_MODEL_CONNECTIONS
        )

        if not isinstance(connection_id, str) or not connection_id:
            raise ArgumentError("connection_id 不能为空")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise ArgumentError("expected_version 必须为 int")

        secret_id_to_delete: str | None = None
        async with SqliteUnitOfWork(self._database) as uow:
            existing = await self._repository.get_by_id(
                uow.connection, connection_id
            )
            if existing is None:
                await uow.rollback()
                raise NotFoundError(
                    "模型连接不存在",
                    context={"connection_id": connection_id},
                )
            secret_id_to_delete = existing.credential_secret_id
            await self._repository.delete_with_expected_version(
                uow.connection, connection_id, expected_version
            )
            await self._append_event(
                uow.connection,
                event_type=_EVENT_DELETED,
                connection_id=connection_id,
                actor_id=actor_id,
                org_id=org_id,
                trace_id=trace_id,
                payload={
                    "connection_id": connection_id,
                },
            )
            await uow.commit()

        # commit 成功：凭据已无引用，best-effort 删除。
        if secret_id_to_delete is not None:
            await self._safe_delete_secret(secret_id_to_delete)

    # ------------------------------------------------------------------ #
    # test_connection
    # ------------------------------------------------------------------ #

    async def test_connection(
        self, connection_id: str, *, actor: ActorContext
    ) -> TestResult:
        """测试连接配置完整性（不含推理调用）。

        验证两项：
        1. 凭据可经 SecretService 解析（purpose=``verify``）；
        2. ``api_base`` 为合法的 http/https URL。

        验证通过 → status=``VERIFIED``；失败 → status=``ERROR``。
        状态更新不递增 ``version_no``（验证动作非配置变更）。
        """
        _require_actor(actor)
        await self._permission_service.require(
            actor, _ACTION_READ, _RESOURCE_MODEL_CONNECTIONS
        )

        if not isinstance(connection_id, str) or not connection_id:
            raise ArgumentError("connection_id 不能为空")

        async with SqliteUnitOfWork(self._database) as uow:
            record = await self._repository.get_by_id(
                uow.connection, connection_id
            )
            await uow.rollback()

        if record is None:
            raise NotFoundError(
                "模型连接不存在",
                context={"connection_id": connection_id},
            )

        now = self._clock.now()
        iso = _ensure_iso(now)

        # 1. 凭据可解析性：service 内部以 connection_id 为 actor_id 解析
        #    （secret owner_id = connection_id，满足 LocalSecretService 默认权限策略）。
        credential_ok = True
        message = "连接配置验证通过"
        if self._secret_service is None:
            credential_ok = False
            message = "SecretService 未注入，无法验证凭据"
        else:
            try:
                await self._secret_service.resolve(
                    record.credential_secret_id, _VERIFY_PURPOSE, connection_id
                )
            except Exception as exc:  # noqa: BLE001 —— 验证失败不抛出，转为结果
                credential_ok = False
                message = f"凭据解析失败：{type(exc).__name__}"

        # 2. URL 格式
        url_ok = _is_valid_url(record.api_base)
        if not url_ok:
            credential_ok = False
            message = "api_base 不是合法的 http/https URL"

        ok = credential_ok and url_ok
        new_status = STATUS_VERIFIED if ok else STATUS_ERROR

        async with SqliteUnitOfWork(self._database) as uow:
            await self._repository.update_status(
                uow.connection, connection_id, new_status, iso
            )
            await uow.commit()

        return TestResult(
            connection_id=connection_id,
            ok=ok,
            status=new_status,
            message=message,
            checked_at=iso,
        )

    # ------------------------------------------------------------------ #
    # verify_connection（TASK-039 分层验证）
    # ------------------------------------------------------------------ #

    async def verify_connection(
        self, connection_id: str, *, actor: ActorContext
    ) -> VerificationResult:
        """分层验证模型连接（config→credential→network→model）。

        实现顺序（对应 TASK-039 任务说明）：
        1. 校验 actor、权限（``read`` ``model_connections``，与 ``test_connection`` 一致）；
        2. 加载 ``ModelConnectionRecord``；不存在抛 ``NotFoundError``；
        3. 委托 ``ModelProbeService.verify(record)`` 执行 4 层验证；
           凭据明文在 probe service 调用栈内短暂存在，绝不进入返回值；
        4. 根据 ``overall_passed`` 更新 ``status``（VERIFIED/ERROR），不递增版本号；
        5. 返回 ``VerificationResult``。

        权限：``read`` ``model_connections``（可验证=可读，OBSERVER 可调用）。
        安全：凭据明文不进入返回值、日志或事件 payload。

        :raises NotFoundError: 连接不存在。
        :raises PermissionDeniedError: 无 ``read`` 权限。
        :raises UnsupportedOperationError: 未注入 ``SecretService`` 且未注入
            ``probe_service``。
        """
        _require_actor(actor)
        await self._permission_service.require(
            actor, _ACTION_READ, _RESOURCE_MODEL_CONNECTIONS
        )

        if not isinstance(connection_id, str) or not connection_id:
            raise ArgumentError("connection_id 不能为空")

        async with SqliteUnitOfWork(self._database) as uow:
            record = await self._repository.get_by_id(
                uow.connection, connection_id
            )
            await uow.rollback()

        if record is None:
            raise NotFoundError(
                "模型连接不存在",
                context={"connection_id": connection_id},
            )

        probe_service = self._get_or_build_probe_service()

        # 凭据明文在 probe_service.verify 调用栈内短暂存在，方法返回前被覆盖。
        result = await probe_service.verify(record)

        # 根据 overall_passed 更新 status；不递增 version_no（验证非配置变更）。
        now = self._clock.now()
        iso = _ensure_iso(now)
        new_status = STATUS_VERIFIED if result["overall_passed"] else STATUS_ERROR
        async with SqliteUnitOfWork(self._database) as uow:
            await self._repository.update_status(
                uow.connection, connection_id, new_status, iso
            )
            await uow.commit()

        return result

    def _get_or_build_probe_service(self) -> ModelProbeService:
        """返回注入的 ``ModelProbeService``，或惰性构造默认实例。

        构造时复用本 service 的 ``secret_service``/``repository``/``clock``
        与默认 ``ProviderAdapterFactory``；测试可通过 ``probe_service`` 参数
        注入自定义实例。
        """
        if self._probe_service is not None:
            return self._probe_service
        factory = self._adapter_factory
        if factory is None:
            factory = ProviderAdapterFactory()
        self._probe_service = ModelProbeService(
            factory=factory,
            secret_service=self._secret_service,
            repository=self._repository,
            clock=self._clock,
        )
        return self._probe_service

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    async def _reload_view(
        self, connection_id: str, *, expected_version: int
    ) -> ModelConnectionView:
        """更新后重新读取记录并返回视图。"""
        async with SqliteUnitOfWork(self._database) as uow:
            record = await self._repository.get_by_id(
                uow.connection, connection_id
            )
            await uow.rollback()
        if record is None:
            raise NotFoundError(
                "模型连接不存在",
                context={"connection_id": connection_id},
            )
        return _record_to_view(record)

    async def _append_event(
        self,
        conn,
        *,
        event_type: str,
        connection_id: str,
        actor_id: str,
        org_id: str,
        trace_id: str,
        payload: dict,
    ) -> None:
        """在同一 UoW 事务内向 Outbox 追加事件。

        事件 payload 不含凭据明文；只记录 connection_id、变更字段与版本。
        """
        publisher = SqliteEventPublisher(conn)
        await publisher.append(
            DomainEvent(
                event_type=event_type,
                aggregate_type=_AGGREGATE_TYPE,
                aggregate_id=connection_id,
                organization_id=org_id,
                actor=ActorRef(actor_type="USER", actor_id=actor_id),
                trace_id=trace_id,
                payload=payload,
            )
        )

    async def _safe_delete_secret(self, secret_id: str) -> None:
        """best-effort 删除 secret；失败吞掉异常，不阻塞主流程。"""
        if self._secret_service is None:
            return
        try:
            await self._secret_service.delete(secret_id)
        except Exception:  # noqa: BLE001 —— best-effort 清理
            pass

    @staticmethod
    def _changed_fields(
        name: str | None, api_base: str | None, credential_value: str | None
    ) -> list[str]:
        changed: list[str] = []
        if name is not None:
            changed.append("name")
        if api_base is not None:
            changed.append("api_base")
        if credential_value is not None:
            changed.append("credential")
        return changed

    @staticmethod
    def _validate_name(name: str) -> str:
        if not isinstance(name, str) or not name.strip():
            raise ArgumentError("name 不能为空")
        return name.strip()

    @staticmethod
    def _validate_required(value: str, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ArgumentError(f"{field} 不能为空")
        return value.strip()

    @staticmethod
    def _validate_provider(provider: str) -> str:
        if not isinstance(provider, str) or provider not in ALLOWED_PROVIDERS:
            raise ArgumentError(
                f"provider 取值非法，允许：{list(ALLOWED_PROVIDERS)}",
                context={"provider": provider},
            )
        return provider

    @staticmethod
    def _validate_credential_type(credential_type: str) -> str:
        if (
            not isinstance(credential_type, str)
            or credential_type not in ALLOWED_CREDENTIAL_TYPES
        ):
            raise ArgumentError(
                f"credential_type 取值非法，允许：{list(ALLOWED_CREDENTIAL_TYPES)}",
                context={"credential_type": credential_type},
            )
        return credential_type


# --------------------------------------------------------------------------- #
# 模块级工具：建表（供测试与开发期首次启动使用）
# --------------------------------------------------------------------------- #


async def ensure_schema(database: Database) -> None:
    """在 ``database`` 上创建 ``model_connections`` 表（幂等）。

    供测试与开发期首次启动使用；正式部署由 ``migrations/`` 顺序迁移负责。
    """
    async with SqliteUnitOfWork(database) as uow:
        await init_schema(uow.connection)
        await uow.commit()


__all__ = [
    "ModelConnectionService",
    "ModelConnectionServiceImpl",
    "PermissionService",
    "ensure_schema",
]
