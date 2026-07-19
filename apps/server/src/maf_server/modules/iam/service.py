"""IAM 应用用例接口与本地登录/登出实现。

TASK-030 范围：
- 保留 ``IamService`` 与 ``PermissionService`` Protocol（其他任务接口契约）。
- 新增 ``IamServiceImpl`` 具体实现 ``login``/``logout``；本任务不实现 RBAC、
  用户管理、系统设置等接口（属 TASK-031/032 范围）。
- ``login`` 顺序：规范化用户名 → 读用户 → 检查 ACTIVE → 恒定时间验证密码 →
  生成 token → 哈希 token 入库 → 更新 ``last_login_at`` → 返回 ``SessionView``。
- ``logout`` 顺序：定位 session → 校验归属 → 标记 ``revoked_at``（幂等）。

安全约束（对应 TASK-030 验收）：
- 错误不区分用户不存在和密码错误：统一抛 ``UnauthenticatedError``，且对不存在
  用户也走一次 ``verify_password`` 恒定时间比较，避免计时侧信道；
- 禁用用户的会话不可继续使用：``logout`` 拒绝 DISABLED 用户的 session；
  认证中间件（后续任务）验证 session 时也会检查 user.status；
- 密码和会话 Token 不写日志：service 不记录 password 与 token 明文；
  审计日志（如启用）只记 user_id、session_id、结果，不记敏感字段。

事务边界：每个用例在 ``SqliteUnitOfWork`` 内执行；``login`` 是单一写事务
（读用户 + 创建 session + 更新 last_login）；``logout`` 是单一写事务
（读 session + 撤销）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal, Protocol

from maf_contracts.common import ActorContext
from maf_contracts.events import ActorRef, DomainEvent
from maf_domain.errors import (
    AlreadyExistsError,
    ArgumentError,
    NotFoundError,
    PermissionDeniedError,
    UnauthenticatedError,
    UnsupportedOperationError,
    VersionConflictError,
)
from maf_policy import (
    CasbinPermissionService,
    KNOWN_ROLES,
    validate_permission_keys,
)

from maf_server.core.clock import Clock
from maf_server.core.database import Database
from maf_server.core.events import SqliteEventPublisher
from maf_server.core.security import (
    DEFAULT_SESSION_TTL_SECONDS,
    compute_session_expiry,
    generate_session_token,
    hash_password,
    hash_session_token,
    verify_password,
)
from maf_server.core.unit_of_work import SqliteUnitOfWork, update_with_expected_version
from maf_server.gateway.secrets.service import SecretService

from .repository import (
    SessionInsert,
    SettingRecord,
    SqliteIamRepository,
    UserRecord,
    init_schema,
    new_session_id,
    new_user_id,
)
from .roles import ACTION_READ, ACTION_WRITE, RESOURCE_SETTINGS, RESOURCE_USERS
from .schemas import (
    CreateUserRequest,
    LoginRequest,
    PutSettingRequest,
    SessionView,
    SettingView,
    UpdateUserRequest,
    UserPage,
    UserQuery,
    UserView,
)
from .settings_schema import SettingSchema, get_setting_schema, validate_value

# --------------------------------------------------------------------------- #
# 内部时钟实现（避免在 maf_server.core.clock 之外引入额外依赖）
# --------------------------------------------------------------------------- #


class _SystemClock:
    """默认使用系统 UTC 时钟；测试可注入虚拟时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def wait_until(self, deadline: datetime) -> None:
        # service 不需要等待；保留 Protocol 兼容。
        return None


# --------------------------------------------------------------------------- #
# Protocol（保留原有接口契约）
# --------------------------------------------------------------------------- #


class IamService(Protocol):
    async def login(self, request: LoginRequest) -> SessionView:
        """验证本地用户名与密码并创建会话。

        输入密码只在内存中用于哈希比较，不能写日志或审计 payload。实现顺序：规范化
        用户名、读取用户、检查 ACTIVE、恒定时间验证密码、创建有过期时间的会话、记录
        登录审计。成功返回会话及脱敏用户；失败统一返回认证失败，避免泄露用户名是否存在。
        """
        ...

    async def logout(self, actor: ActorContext, session_id: str) -> None:
        """撤销当前会话。

        只能撤销调用者自己的 session。重复注销视为成功；提交后该 session 的后续请求必须
        返回未认证。产生 ``auth.session.revoked`` 审计记录，不删除历史。
        """
        ...

    async def get_current_user(self, actor: ActorContext) -> UserView:
        """返回当前用户与实时有效权限，不信任浏览器缓存的权限列表。"""
        ...

    async def list_users(self, actor: ActorContext, query: UserQuery) -> UserPage:
        """分页查询单组织用户；先校验管理权限，再应用状态和关键字过滤。"""
        ...

    async def create_user(self, actor: ActorContext, request: CreateUserRequest) -> UserView:
        """创建本地用户。

        校验管理员权限、用户名唯一性、密码规则及权限键有效性；密码必须先做强哈希再保存。
        同一幂等键不得创建两个用户。输出不包含密码哈希，产生 ``iam.user.created`` 事件。
        """
        ...

    async def update_user(
        self, actor: ActorContext, user_id: str, request: UpdateUserRequest
    ) -> UserView:
        """按 expected_version 更新用户资料、状态或权限。

        禁止操作者禁用系统最后一个管理员。版本不一致返回冲突；禁用后撤销目标用户全部
        会话。输出为更新后的用户视图并产生审计事件。
        """
        ...

    async def get_setting(self, actor: ActorContext, key: str) -> SettingView:
        """读取允许公开给当前管理员的系统设置；Secret 只返回是否已配置。"""
        ...

    async def put_setting(
        self, actor: ActorContext, key: str, request: PutSettingRequest
    ) -> SettingView:
        """创建或更新一个受支持的系统设置。

        按 key 查找预定义 Schema，验证值、权限、expected_version 和幂等键；敏感配置必须
        转存 SecretStore，业务表仅保存引用。返回新版本，不返回密钥明文。
        """
        ...


class PermissionService(Protocol):
    async def require(self, actor: ActorContext, action: str, resource: str) -> None:
        """无返回表示允许；无匹配授权、上下文缺失或策略异常都必须拒绝。"""
        ...

    async def list_effective_permissions(self, actor: ActorContext) -> list[str]:
        """计算当前主体的实时有效权限键，供 `/me` 和界面显示使用。"""
        ...


# --------------------------------------------------------------------------- #
# IamServiceImpl 具体实现
# --------------------------------------------------------------------------- #


# 认证失败统一错误信息，避免泄露用户是否存在或状态。
_AUTH_FAILED_MESSAGE: str = "用户名或密码错误"


def _normalize_username(username: str) -> str:
    """规范化用户名：去除首尾空白。

    TASK-030 不强制大小写折叠（保留 username 大小写敏感），仅去除首尾空白，
    与 ``users.username UNIQUE`` 约束配合。
    """
    if not isinstance(username, str):
        raise UnauthenticatedError(_AUTH_FAILED_MESSAGE)
    return username.strip()


def _user_record_to_view(user: UserRecord, permissions: list[str] | None = None) -> UserView:
    """把 ``UserRecord`` 映射为对外 ``UserView``，不含密码哈希。"""
    # 显式收敛到 Literal["ACTIVE", "DISABLED"]，避免 mypy 类型 narrowing 失败。
    status_value: Literal["ACTIVE", "DISABLED"] = (
        "ACTIVE" if user.status == "ACTIVE" else "DISABLED"
    )
    return UserView(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        status=status_value,
        permissions=list(permissions or []),
        version=user.version_no,
    )


def _ensure_iso(value: datetime) -> str:
    """把 datetime 序列化为带时区 ISO 8601 字符串。naive 视为 UTC。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _fingerprint(plaintext: str) -> str:
    """不可逆指纹：``sha256(plaintext)[:8] + ".." + plaintext[-4:]``。

    与 ``LocalSecretService._fingerprint`` 实现一致，用于敏感设置的运维展示与
    去重识别；指纹后 4 位为明文末尾（设计文档 §25.1 允许），不构成前缀泄露。
    本函数在 service 层本地计算，避免依赖 SecretService Protocol 暴露 metadata。
    """
    digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    tail = plaintext[-4:] if len(plaintext) >= 4 else plaintext
    return f"{digest[:8]}..{tail}"


def _require_actor(actor: ActorContext) -> tuple[str, str, str]:
    """校验 actor 并返回 (user_id, organization_id, trace_id)。

    未认证抛 ``UnauthenticatedError``。组织 ID 缺失时回退为 ``"system"``，
    供 SecretService owner 与事件 organization_id 使用。
    """
    if not isinstance(actor, dict):
        raise UnauthenticatedError("未认证")
    user_id = actor.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        raise UnauthenticatedError("未认证")
    organization_id = actor.get("organization_id")
    if not isinstance(organization_id, str) or not organization_id:
        organization_id = "system"
    trace_id = actor.get("trace_id")
    if not isinstance(trace_id, str):
        trace_id = ""
    return user_id, organization_id, trace_id


class IamServiceImpl:
    """``IamService`` 的本地登录/登出实现。

    依赖注入：
        - ``database``：``Database``，提供 ``SqliteUnitOfWork`` 事务边界；
        - ``repository``：``SqliteIamRepository``，IAM 表 CRUD；
        - ``clock``：``Clock``，可注入虚拟时钟用于测试过期；
        - ``session_ttl_seconds``：会话存活秒数，默认 12 小时。

    本类不实现 RBAC（TASK-031 范围）；``permissions`` 字段在 TASK-030 中始终返回
    空列表，待 TASK-031 实现 ``PermissionService`` 后补充。

    TASK-031 扩展：
        - 注入 ``PermissionService``（默认 ``CasbinPermissionService``），
          在 ``create_user``/``update_user``/``list_users`` 中校验管理员权限；
        - 实现 ``create_user``/``update_user``/``list_users``/``get_current_user``；
        - ``login`` 返回的 ``UserView.permissions`` 从 ``user_permissions`` 表加载；
        - ``update_user`` 在禁用或移除最后一个 ADMIN 时拒绝操作。

    安全约束：
        - 明文密码与明文 token 绝不进入实例属性、日志或异常 context；
        - ``login`` 对"用户不存在"也执行一次 ``verify_password`` 恒定时间比较，
          避免通过响应耗时区分用户是否存在；
        - ``logout`` 校验 session 归属当前 actor，拒绝跨用户撤销。
    """

    def __init__(
        self,
        database: Database,
        repository: SqliteIamRepository | None = None,
        clock: Clock | None = None,
        session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        permission_service: "PermissionService | None" = None,
        secret_service: SecretService | None = None,
    ) -> None:
        self._database: Database = database
        self._repository: SqliteIamRepository = repository or SqliteIamRepository()
        self._clock: Clock = clock or _SystemClock()
        self._session_ttl_seconds: int = session_ttl_seconds
        self._permission_service: PermissionService = (
            permission_service or CasbinPermissionService()
        )
        self._secret_service: SecretService | None = secret_service

    # ------------------------------------------------------------------ #
    # login
    # ------------------------------------------------------------------ #

    async def login(self, request: LoginRequest) -> SessionView:
        """验证本地用户名密码并创建会话。

        实现顺序（对应《接口设计与实现规范》第 6 节）：
        1. 规范化 username，校验非空；
        2. 进入 UoW 写事务；
        3. 读用户记录（含 ``password_hash``）；
        4. 若用户不存在：仍执行一次 ``verify_password`` 恒定时间比较，
           避免计时侧信道；随后抛 ``UnauthenticatedError``；
        5. 若用户存在但 DISABLED：同样执行恒定时间比较后抛
           ``UnauthenticatedError``（不区分错误类型）；
        6. 恒定时间验证密码，不匹配抛 ``UnauthenticatedError``；
        7. 生成 Session Token（``secrets.token_urlsafe``），SHA-256 哈希后入库；
        8. 插入 ``sessions`` 行，``revoked_at`` 为 NULL；
        9. 更新 ``users.last_login_at``；
        10. commit；
        11. 返回 ``SessionView``（含明文 token，仅本次响应返回）。

        输入来源与可信度：
            - ``request`` 来自 HTTP 请求体，不可信；
            - ``username``/``password`` 仅在内存中用于哈希比较。

        可能业务错误：
            - ``UnauthenticatedError``（HTTP 401）：用户不存在、密码错误、
              用户禁用统一返回该错误，错误信息固定为"用户名或密码错误"。

        安全约束：
            - 明文密码不写日志、不进异常 context；
            - 明文 token 不写日志、不进异常 context；
            - 异常路径不区分用户是否存在或状态。

        :param request: ``LoginRequest``，含 ``username`` 与 ``password``。
        :returns: ``SessionView``，含 ``token``（仅本次响应返回）。
        :raises UnauthenticatedError: 认证失败。
        """
        username_raw = request.get("username", "") if isinstance(request, dict) else ""
        password_raw = request.get("password", "") if isinstance(request, dict) else ""

        if not isinstance(username_raw, str) or not isinstance(password_raw, str):
            raise UnauthenticatedError(_AUTH_FAILED_MESSAGE)

        username = _normalize_username(username_raw)
        if not username or not password_raw:
            raise UnauthenticatedError(_AUTH_FAILED_MESSAGE)

        now = self._clock.now()
        expires_at = compute_session_expiry(now, self._session_ttl_seconds)

        async with SqliteUnitOfWork(self._database) as uow:
            user = await self._repository.get_user_by_username(uow.connection, username)

            # 防御性恒定时间比较：用户不存在时也对一个虚拟哈希做 verify，
            # 让响应耗时与"密码错误"分支接近，避免用户枚举。
            if user is None:
                _ = verify_password(password_raw, _DUMMY_BCRYPT_HASH)
                await uow.rollback()
                raise UnauthenticatedError(_AUTH_FAILED_MESSAGE)

            if user.status != "ACTIVE":
                # 禁用用户：仍走恒定时间密码验证，避免与"密码错误"分支差异。
                _ = verify_password(password_raw, user.password_hash)
                await uow.rollback()
                raise UnauthenticatedError(_AUTH_FAILED_MESSAGE)

            if not verify_password(password_raw, user.password_hash):
                await uow.rollback()
                raise UnauthenticatedError(_AUTH_FAILED_MESSAGE)

            # 生成 token，明文仅返回给调用方；库中只存 SHA-256。
            token_plain = generate_session_token()
            token_hash = hash_session_token(token_plain)
            session_id = new_session_id()

            await self._repository.create_session(
                uow.connection,
                SessionInsert(
                    id=session_id,
                    user_id=user.id,
                    token_hash=token_hash,
                    created_at=_ensure_iso(now),
                    expires_at=_ensure_iso(expires_at),
                    user_agent=None,
                ),
            )
            await self._repository.update_last_login(uow.connection, user.id, now)
            # TASK-031: 加载用户权限（角色列表），供 SessionView 返回。
            permissions = await self._repository.get_user_permissions(
                uow.connection, user.id
            )
            await uow.commit()

        user_view = _user_record_to_view(user, permissions=permissions)
        return SessionView(
            session_id=session_id,
            expires_at=_ensure_iso(expires_at),
            token=token_plain,
            user=user_view,
        )

    # ------------------------------------------------------------------ #
    # logout
    # ------------------------------------------------------------------ #

    async def logout(self, actor: ActorContext, session_id: str) -> None:
        """撤销当前会话；幂等。

        实现顺序：
        1. 校验 ``actor`` 必填字段（user_id、trace_id）；
        2. 进入 UoW 写事务；
        3. 按 ``session_id`` 读会话；
        4. 不存在视为已注销（幂等），直接返回；
        5. 校验 session 归属当前 actor（``session.user_id == actor.user_id``）；
           不匹配抛 ``UnauthenticatedError``（不暴露 session 是否存在）；
        6. 校验用户状态：DISABLED 用户的 session 不可继续使用，但仍允许撤销（幂等返回）；
        7. 设置 ``revoked_at``；重复撤销（已非空）视为成功；
        8. commit。

        输入来源与可信度：
            - ``actor`` 由认证中间件构造，可信；
            - ``session_id`` 由客户端从 Cookie 或响应体取得，不可信。

        可能业务错误：
            - ``UnauthenticatedError``（HTTP 401）：actor 缺失或 session 不属于
              当前 actor。

        安全约束：
            - 不删除 session 历史（审计需要），只标记 ``revoked_at``；
            - 重复注销不抛错（幂等）；
            - 明文 token 不进入本方法（只需 session_id）。

        :param actor: 当前调用者上下文。
        :param session_id: 待撤销会话 ID。
        :raises UnauthenticatedError: actor 无效或 session 归属不匹配。
        """
        if not isinstance(actor, dict):
            raise UnauthenticatedError("未认证")
        actor_user_id = actor.get("user_id")
        if not actor_user_id or not isinstance(actor_user_id, str):
            raise UnauthenticatedError("未认证")
        if not session_id or not isinstance(session_id, str):
            raise UnauthenticatedError("未认证")

        now = self._clock.now()
        async with SqliteUnitOfWork(self._database) as uow:
            session = await self._repository.get_session_by_id(
                uow.connection, session_id
            )
            if session is None:
                # 幂等：session 不存在视为已注销，直接返回。
                await uow.rollback()
                return

            if session.user_id != actor_user_id:
                # 不暴露 session 是否存在；统一返回未认证。
                await uow.rollback()
                raise UnauthenticatedError("未认证")

            # 标记 revoked_at；重复撤销视为成功。
            await self._repository.revoke_session(
                uow.connection, session_id, now
            )
            await uow.commit()

    # ------------------------------------------------------------------ #
    # 用户管理与 RBAC（TASK-031）
    # ------------------------------------------------------------------ #

    async def get_current_user(self, actor: ActorContext) -> UserView:
        """返回当前用户的实时数据，不信任浏览器缓存的权限列表。

        从 DB 重新加载用户记录和 permission_keys，确保权限撤销立即生效。
        """
        if not isinstance(actor, dict):
            raise UnauthenticatedError("未认证")
        user_id = actor.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise UnauthenticatedError("未认证")

        async with SqliteUnitOfWork(self._database) as uow:
            user = await self._repository.get_user_by_id(uow.connection, user_id)
            if user is None:
                await uow.rollback()
                raise NotFoundError("用户不存在")
            permissions = await self._repository.get_user_permissions(
                uow.connection, user_id
            )
            await uow.rollback()
        return _user_record_to_view(user, permissions=permissions)

    async def list_users(self, actor: ActorContext, query: UserQuery) -> UserPage:
        """分页查询用户列表；需要 ADMIN 权限。"""
        await self._permission_service.require(actor, ACTION_READ, RESOURCE_USERS)

        cursor = query.get("cursor") if isinstance(query, dict) else None
        limit = query.get("limit", 50) if isinstance(query, dict) else 50
        status_filter = query.get("status") if isinstance(query, dict) else None
        keyword = query.get("keyword") if isinstance(query, dict) else None

        if not isinstance(limit, int) or limit <= 0:
            limit = 50
        if cursor is not None and not isinstance(cursor, str):
            cursor = None
        if status_filter is not None and status_filter not in ("ACTIVE", "DISABLED"):
            status_filter = None
        if keyword is not None and not isinstance(keyword, str):
            keyword = None

        async with SqliteUnitOfWork(self._database) as uow:
            records, next_cursor = await self._repository.list_users_page(
                uow.connection,
                cursor=cursor,
                limit=limit,
                status_filter=status_filter,
                keyword=keyword,
            )
            items: list[UserView] = []
            for rec in records:
                perms = await self._repository.get_user_permissions(
                    uow.connection, rec.id
                )
                items.append(_user_record_to_view(rec, permissions=perms))
            await uow.rollback()

        return UserPage(
            items=items,
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    async def create_user(self, actor: ActorContext, request: CreateUserRequest) -> UserView:
        """创建本地用户；需要 ADMIN 权限。

        校验：管理员权限、用户名唯一性、密码非空、permission_keys 有效。
        密码强哈希后入库；明文密码绝不持久化。
        """
        await self._permission_service.require(actor, ACTION_WRITE, RESOURCE_USERS)

        if not isinstance(request, dict):
            raise ArgumentError("请求体无效")

        username = request.get("username", "")
        display_name = request.get("display_name", "")
        password_plain = request.get("initial_password", "")
        raw_keys = request.get("permission_keys", [])

        if not isinstance(username, str) or not username.strip():
            raise ArgumentError("用户名不能为空")
        if not isinstance(display_name, str) or not display_name.strip():
            raise ArgumentError("显示名不能为空")
        if not isinstance(password_plain, str) or not password_plain:
            raise ArgumentError("密码不能为空")
        if not isinstance(raw_keys, list):
            raise ArgumentError("permission_keys 必须是列表")

        username = username.strip()
        display_name = display_name.strip()
        try:
            permission_keys = validate_permission_keys(raw_keys)
        except ValueError as exc:
            raise ArgumentError(str(exc)) from exc

        now = self._clock.now()
        uid = new_user_id()
        pw_hash = hash_password(password_plain)

        async with SqliteUnitOfWork(self._database) as uow:
            existing = await self._repository.get_user_by_username(
                uow.connection, username
            )
            if existing is not None:
                await uow.rollback()
                raise AlreadyExistsError(
                    "用户名已存在", context={"username": username}
                )

            await self._repository.create_user_record(
                uow.connection,
                user_id=uid,
                username=username,
                display_name=display_name,
                password_hash=pw_hash,
                status="ACTIVE",
                at=now,
            )
            if permission_keys:
                await self._repository.replace_user_permissions(
                    uow.connection, uid, permission_keys, now
                )
            await uow.commit()

        return UserView(
            id=uid,
            username=username,
            display_name=display_name,
            status="ACTIVE",
            permissions=list(permission_keys),
            version=1,
        )

    async def update_user(
        self, actor: ActorContext, user_id: str, request: UpdateUserRequest
    ) -> UserView:
        """按 expected_version 更新用户资料、状态或权限；需要 ADMIN 权限。

        - 禁止操作者禁用系统最后一个管理员；
        - 禁用用户后撤销其全部会话；
        - 版本不一致返回冲突。
        """
        await self._permission_service.require(actor, ACTION_WRITE, RESOURCE_USERS)

        if not isinstance(user_id, str) or not user_id:
            raise ArgumentError("user_id 不能为空")
        if not isinstance(request, dict):
            raise ArgumentError("请求体无效")

        display_name = request.get("display_name")
        new_status = request.get("status")
        raw_keys = request.get("permission_keys")
        expected_version = request.get("expected_version")

        if display_name is not None and (
            not isinstance(display_name, str) or not display_name.strip()
        ):
            raise ArgumentError("显示名不能为空")
        if new_status is not None and new_status not in ("ACTIVE", "DISABLED"):
            raise ArgumentError("status 取值非法")
        if raw_keys is not None and not isinstance(raw_keys, list):
            raise ArgumentError("permission_keys 必须是列表")

        new_keys: list[str] | None = None
        if raw_keys is not None:
            try:
                new_keys = validate_permission_keys(raw_keys)
            except ValueError as exc:
                raise ArgumentError(str(exc)) from exc

        now = self._clock.now()
        async with SqliteUnitOfWork(self._database) as uow:
            user = await self._repository.get_user_by_id(uow.connection, user_id)
            if user is None:
                await uow.rollback()
                raise NotFoundError("用户不存在", context={"user_id": user_id})

            if expected_version is not None and user.version_no != expected_version:
                await uow.rollback()
                raise VersionConflictError(
                    "版本不一致",
                    context={
                        "expected": expected_version,
                        "actual": user.version_no,
                    },
                )

            current_keys = await self._repository.get_user_permissions(
                uow.connection, user_id
            )

            # 最后一个管理员保护：
            # 若要禁用 ADMIN 用户或从 ADMIN 用户移除 ADMIN 角色，
            # 检查是否还剩其他 ACTIVE ADMIN。
            will_lose_admin = False
            if new_status == "DISABLED" and "ADMIN" in current_keys:
                will_lose_admin = True
            if (
                new_keys is not None
                and "ADMIN" in current_keys
                and "ADMIN" not in new_keys
            ):
                will_lose_admin = True

            if will_lose_admin:
                admin_count = (
                    await self._repository.count_active_users_with_permission(
                        uow.connection, "ADMIN"
                    )
                )
                if admin_count <= 1:
                    await uow.rollback()
                    raise ArgumentError(
                        "不能禁用或移除最后一个管理员",
                        context={"user_id": user_id},
                    )

            new_version = await self._repository.update_user_fields(
                uow.connection,
                user_id,
                display_name=display_name,
                status=new_status,
                at=now,
            )
            if new_version == 0:
                await uow.rollback()
                raise NotFoundError("用户不存在", context={"user_id": user_id})

            if new_keys is not None:
                await self._repository.replace_user_permissions(
                    uow.connection, user_id, new_keys, now
                )
                final_keys = new_keys
            else:
                final_keys = current_keys

            # 禁用用户时撤销全部会话
            if new_status == "DISABLED":
                await self._repository.revoke_sessions_for_user(
                    uow.connection, user_id, now
                )

            await uow.commit()

        # 重新读取用户记录以获取更新后的字段
        async with SqliteUnitOfWork(self._database) as uow:
            updated_user = await self._repository.get_user_by_id(
                uow.connection, user_id
            )
            await uow.rollback()

        if updated_user is None:
            raise NotFoundError("用户不存在", context={"user_id": user_id})

        return _user_record_to_view(updated_user, permissions=final_keys)

    # ------------------------------------------------------------------ #
    # 系统设置占位（TASK-032 范围）
    # ------------------------------------------------------------------ #

    async def get_setting(self, actor: ActorContext, key: str) -> SettingView:
        """读取系统设置视图。

        - 校验 actor 已认证、key 在预定义 Schema 中、``read`` ``settings`` 权限；
        - 未配置时返回 ``configured=False`` 与 Schema 默认值（敏感设置 value=None）；
        - 敏感设置绝不返回明文，只返回 ``configured`` 与 ``fingerprint``。
        """
        actor_id, _org_id, _trace_id = _require_actor(actor)

        schema = get_setting_schema(key)
        if schema is None:
            raise ArgumentError(f"未知的设置 key: {key!r}", context={"key": key})

        await self._permission_service.require(actor, ACTION_READ, RESOURCE_SETTINGS)

        async with SqliteUnitOfWork(self._database) as uow:
            record = await self._repository.get_setting(uow.connection, key)
            await uow.rollback()

        if record is None:
            return SettingView(
                key=key,
                value=None if schema.is_secret else schema.default,
                value_type=schema.value_type,
                is_secret=schema.is_secret,
                configured=False,
                fingerprint=None,
                version=0,
                updated_at="",
                updated_by="",
            )

        if record.is_secret:
            value: object = None
            configured = record.secret_id is not None
            fingerprint = record.fingerprint
        else:
            value = (
                json.loads(record.value) if record.value is not None else None
            )
            configured = True
            fingerprint = None

        return SettingView(
            key=record.key,
            value=value,
            value_type=record.value_type,
            is_secret=record.is_secret,
            configured=configured,
            fingerprint=fingerprint,
            version=record.version_no,
            updated_at=record.updated_at,
            updated_by=record.updated_by,
        )

    async def put_setting(
        self, actor: ActorContext, key: str, request: PutSettingRequest
    ) -> SettingView:
        """创建或乐观锁更新系统设置。

        - 仅 ADMIN 可调用（``write`` ``settings``，DEFAULT_POLICIES 中仅 ADMIN 拥有）；
        - 未知 key 抛 ``ArgumentError``；
        - 敏感设置：明文经 ``SecretService.create`` 写入后端（UoW 之外），
          SQLite 仅保存 ``secret_id`` 与本地计算的 ``fingerprint``；
          旧 secret 在新设置提交后 best-effort 删除；UoW 失败时 best-effort 删除新 secret。
        - 非敏感设置：JSON 序列化后直接写入 ``value`` 列；
        - 写入成功在同一事务内追加 ``system.setting.changed`` Outbox 事件；
        - ``expected_version`` 不匹配抛 ``VersionConflictError``（HTTP 409）。
        """
        actor_id, org_id, trace_id = _require_actor(actor)

        schema = get_setting_schema(key)
        if schema is None:
            raise ArgumentError(f"未知的设置 key: {key!r}", context={"key": key})

        await self._permission_service.require(actor, ACTION_WRITE, RESOURCE_SETTINGS)

        if not isinstance(request, dict):
            raise ArgumentError("请求体无效")
        raw_value = request.get("value")
        expected_version = request.get("expected_version")
        if expected_version is not None and not isinstance(expected_version, int):
            raise ArgumentError("expected_version 必须为 int 或 None")
        if isinstance(expected_version, bool):
            raise ArgumentError("expected_version 必须为 int 或 None")

        validated = validate_value(schema, raw_value)

        now = self._clock.now()
        iso = _ensure_iso(now)

        if schema.is_secret:
            return await self._put_secret_setting(
                schema=schema,
                plaintext=validated,
                key=key,
                actor_id=actor_id,
                org_id=org_id,
                trace_id=trace_id,
                expected_version=expected_version,
                iso=iso,
            )
        return await self._put_plain_setting(
            schema=schema,
            validated=validated,
            key=key,
            actor_id=actor_id,
            org_id=org_id,
            trace_id=trace_id,
            expected_version=expected_version,
            iso=iso,
        )

    async def _put_plain_setting(
        self,
        *,
        schema: SettingSchema,
        validated: object,
        key: str,
        actor_id: str,
        org_id: str,
        trace_id: str,
        expected_version: int | None,
        iso: str,
    ) -> SettingView:
        """非敏感设置写入路径：JSON 序列化后直接存 ``value`` 列。"""
        serialized = json.dumps(validated, ensure_ascii=False, default=str)

        async with SqliteUnitOfWork(self._database) as uow:
            existing = await self._repository.get_setting(uow.connection, key)

            if existing is None:
                if expected_version is not None:
                    await uow.rollback()
                    raise VersionConflictError(
                        f"设置 {key!r} 不存在，expected_version={expected_version} 不匹配",
                        context={
                            "key": key,
                            "expected": expected_version,
                            "actual": 0,
                        },
                        retryable=True,
                    )
                await self._repository.insert_setting(
                    uow.connection,
                    key=key,
                    value=serialized,
                    value_type=schema.value_type,
                    is_secret=False,
                    secret_id=None,
                    fingerprint=None,
                    updated_at=iso,
                    updated_by=actor_id,
                )
                new_version = 1
            else:
                if existing.is_secret:
                    await uow.rollback()
                    raise ArgumentError(
                        f"设置 {key!r} 已存在且为敏感类型，不能转为非敏感",
                        context={"key": key},
                    )
                if expected_version is not None and existing.version_no != expected_version:
                    await uow.rollback()
                    raise VersionConflictError(
                        f"设置 {key!r} 版本不匹配",
                        context={
                            "key": key,
                            "expected": expected_version,
                            "actual": existing.version_no,
                        },
                        retryable=True,
                    )
                await update_with_expected_version(
                    uow.connection,
                    "system_settings",
                    assignments={
                        "value": serialized,
                        "secret_id": None,
                        "fingerprint": None,
                        "updated_at": iso,
                        "updated_by": actor_id,
                    },
                    where={"key": key},
                    expected_version=existing.version_no,
                )
                new_version = existing.version_no + 1

            await self._append_setting_changed_event(
                uow.connection,
                key=key,
                is_secret=False,
                new_version=new_version,
                actor_id=actor_id,
                org_id=org_id,
                trace_id=trace_id,
            )
            await uow.commit()

        return SettingView(
            key=key,
            value=validated,
            value_type=schema.value_type,
            is_secret=False,
            configured=True,
            fingerprint=None,
            version=new_version,
            updated_at=iso,
            updated_by=actor_id,
        )

    async def _put_secret_setting(
        self,
        *,
        schema: SettingSchema,
        plaintext: str,
        key: str,
        actor_id: str,
        org_id: str,
        trace_id: str,
        expected_version: int | None,
        iso: str,
    ) -> SettingView:
        """敏感设置写入路径：明文经 SecretService 存储，SQLite 只保存引用。

        策略：
        1. UoW 之外调用 ``secret_service.create`` 创建新 secret（短事务原则）；
        2. UoW 内：读旧记录、校验 expected_version、INSERT 或乐观锁 UPDATE，
           追加事件，commit；
        3. commit 成功后 best-effort 删除旧 secret；失败时 best-effort 删除新 secret。
        """
        if self._secret_service is None:
            raise UnsupportedOperationError(
                f"敏感设置 {key!r} 需要 SecretService，但未注入",
                context={"key": key},
            )

        new_secret_id = await self._secret_service.create(
            owner_type="system",
            owner_id=org_id,
            plaintext=plaintext,
        )
        new_fingerprint = _fingerprint(plaintext)
        old_secret_id: str | None = None
        new_version = 0

        try:
            async with SqliteUnitOfWork(self._database) as uow:
                existing = await self._repository.get_setting(
                    uow.connection, key
                )

                if existing is None:
                    if expected_version is not None:
                        await uow.rollback()
                        raise VersionConflictError(
                            f"设置 {key!r} 不存在，expected_version={expected_version} 不匹配",
                            context={
                                "key": key,
                                "expected": expected_version,
                                "actual": 0,
                            },
                            retryable=True,
                        )
                    await self._repository.insert_setting(
                        uow.connection,
                        key=key,
                        value=None,
                        value_type=schema.value_type,
                        is_secret=True,
                        secret_id=new_secret_id,
                        fingerprint=new_fingerprint,
                        updated_at=iso,
                        updated_by=actor_id,
                    )
                    new_version = 1
                else:
                    if not existing.is_secret:
                        await uow.rollback()
                        raise ArgumentError(
                            f"设置 {key!r} 已存在且非敏感类型，不能转为敏感",
                            context={"key": key},
                        )
                    if (
                        expected_version is not None
                        and existing.version_no != expected_version
                    ):
                        await uow.rollback()
                        raise VersionConflictError(
                            f"设置 {key!r} 版本不匹配",
                            context={
                                "key": key,
                                "expected": expected_version,
                                "actual": existing.version_no,
                            },
                            retryable=True,
                        )
                    old_secret_id = existing.secret_id
                    await update_with_expected_version(
                        uow.connection,
                        "system_settings",
                        assignments={
                            "value": None,
                            "secret_id": new_secret_id,
                            "fingerprint": new_fingerprint,
                            "updated_at": iso,
                            "updated_by": actor_id,
                        },
                        where={"key": key},
                        expected_version=existing.version_no,
                    )
                    new_version = existing.version_no + 1

                await self._append_setting_changed_event(
                    uow.connection,
                    key=key,
                    is_secret=True,
                    new_version=new_version,
                    actor_id=actor_id,
                    org_id=org_id,
                    trace_id=trace_id,
                )
                await uow.commit()
        except BaseException:
            # UoW 失败：新 secret 已无引用，best-effort 删除，不阻塞原异常。
            try:
                await self._secret_service.delete(new_secret_id)
            except Exception:
                pass
            raise

        # commit 成功：旧 secret 已无引用，best-effort 删除。
        if old_secret_id is not None:
            try:
                await self._secret_service.delete(old_secret_id)
            except Exception:
                pass

        return SettingView(
            key=key,
            value=None,
            value_type=schema.value_type,
            is_secret=True,
            configured=True,
            fingerprint=new_fingerprint,
            version=new_version,
            updated_at=iso,
            updated_by=actor_id,
        )

    async def _append_setting_changed_event(
        self,
        conn,
        *,
        key: str,
        is_secret: bool,
        new_version: int,
        actor_id: str,
        org_id: str,
        trace_id: str,
    ) -> None:
        """在同一 UoW 事务内向 Outbox 追加 ``system.setting.changed`` 事件。

        事件 payload 不含明文；敏感设置只记录 key、版本与 updated_by。
        """
        publisher = SqliteEventPublisher(conn)
        await publisher.append(
            DomainEvent(
                event_type="system.setting.changed",
                aggregate_type="system_setting",
                aggregate_id=key,
                organization_id=org_id,
                actor=ActorRef(actor_type="USER", actor_id=actor_id),
                trace_id=trace_id,
                payload={
                    "key": key,
                    "is_secret": is_secret,
                    "version": new_version,
                    "updated_by": actor_id,
                },
            )
        )


# --------------------------------------------------------------------------- #
# 模块级工具：建表与种子用户（供测试与开发期首次启动使用）
# --------------------------------------------------------------------------- #


# 一个合法的 bcrypt 哈希，用于"用户不存在"分支的恒定时间比较。
# 哈希内容不重要，只要格式合法让 passlib 走完整 verify 流程即可。
# 该值不与任何真实密码匹配。
_DUMMY_BCRYPT_HASH: str = (
    "$2b$12$0123456789012345678901uPxFQ/HQjxv/XuRBKvDqY3lRvY3nNqW"
)


async def ensure_schema(database: Database) -> None:
    """在 ``database`` 上创建 IAM 表（幂等）。

    供测试与开发期首次启动使用；正式部署由 ``migrations/`` 顺序迁移负责。
    本函数在独立写事务中执行 ``CREATE TABLE IF NOT EXISTS``。
    """
    async with SqliteUnitOfWork(database) as uow:
        await init_schema(uow.connection)
        await uow.commit()


async def seed_local_user(
    database: Database,
    *,
    repository: SqliteIamRepository | None = None,
    username: str,
    display_name: str,
    password_plain: str,
    user_id: str | None = None,
    status: str = "ACTIVE",
    permission_keys: list[str] | None = None,
) -> str:
    """插入一个本地用户并返回其 id（开发期种子 / 测试辅助）。

    生产环境应通过 ``IamService.create_user``（TASK-031）创建用户；
    本函数仅供测试与开发期首次启动初始化管理员账户使用。

    TASK-031 扩展：``permission_keys`` 可选，用于种子管理员的角色。
    明文密码 ``password_plain`` 仅在内存中哈希，绝不持久化、绝不写日志。
    """
    import uuid as _uuid

    uid = user_id or str(_uuid.uuid4())
    now = datetime.now(timezone.utc)
    iso = _ensure_iso(now)
    pw_hash = hash_password(password_plain)
    repo = repository or SqliteIamRepository()

    async with SqliteUnitOfWork(database) as uow:
        await uow.connection.execute(
            "INSERT INTO users (id, username, display_name, password_hash, "
            "email, status, last_login_at, created_at, updated_at, version_no) "
            "VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?, 1)",
            (uid, username, display_name, pw_hash, status, iso, iso),
        )
        if permission_keys:
            await repo.replace_user_permissions(
                uow.connection, uid, permission_keys, now
            )
        await uow.commit()
    return uid


__all__ = [
    "IamService",
    "IamServiceImpl",
    "PermissionService",
    "ensure_schema",
    "seed_local_user",
]
