"""IAM 持久化接口与 SQLite 实现。

TASK-030 范围：
- 保留 ``IamRepository`` Protocol（其他任务接口契约）。
- 新增 ``SqliteIamRepository`` 具体实现，负责 ``users`` 与 ``sessions`` 两张表的
  CRUD；密码哈希不存明文（由 ``core.security.hash_password`` 保证），Session Token
  哈希不存明文（由 ``core.security.hash_session_token`` 保证）。
- 新增 ``UserRecord``、``SessionRecord`` dataclass 作为行映射，供 service 层使用，
  不直接对外暴露（API 层使用 ``schemas.UserView``/``SessionView``）。
- 提供 ``SCHEMA_SQL`` 与 ``init_schema`` 用于在测试与首次启动时建立 IAM 表结构；
  正式部署应由 ``migrations/`` 顺序迁移负责，本常量仅在 TASK-030 范围内供测试使用，
  避免修改 migrations 目录。

表结构遵循设计文档 7.1 节，并补充 sessions 表：
- ``users``：id/username/display_name/password_hash/email/status/last_login_at/
  created_at/updated_at/version_no；
- ``sessions``：id/user_id/token_hash/created_at/expires_at/revoked_at/
  last_used_at/user_agent；token_hash 为 SHA-256，明文 token 不入库；
  ``revoked_at`` 非空表示会话已注销。

事务边界：repository 方法接受 ``aiosqlite.Connection``（由 ``SqliteUnitOfWork``
提供），不自开事务；service 层负责 ``BEGIN IMMEDIATE``/``COMMIT``。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

import aiosqlite

from .schemas import SettingView, UserPage, UserQuery, UserView

# --------------------------------------------------------------------------- #
# 表结构 DDL（供测试与首次启动建表使用；正式部署由 migrations 负责）
# --------------------------------------------------------------------------- #

SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT    PRIMARY KEY,
    username      TEXT    NOT NULL UNIQUE,
    display_name  TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    email         TEXT,
    status        TEXT    NOT NULL DEFAULT 'ACTIVE',
    last_login_at TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    version_no    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT    PRIMARY KEY,
    user_id       TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash    TEXT    NOT NULL UNIQUE,
    created_at    TEXT    NOT NULL,
    expires_at    TEXT    NOT NULL,
    revoked_at    TEXT,
    last_used_at  TEXT,
    user_agent    TEXT
);

CREATE TABLE IF NOT EXISTS user_permissions (
    id              TEXT    PRIMARY KEY,
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission_key  TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    UNIQUE(user_id, permission_key)
);

CREATE TABLE IF NOT EXISTS system_settings (
    key            TEXT    PRIMARY KEY,
    value          TEXT,
    value_type     TEXT    NOT NULL,
    is_secret      INTEGER NOT NULL DEFAULT 0,
    secret_id      TEXT,
    fingerprint    TEXT,
    updated_at     TEXT    NOT NULL,
    updated_by     TEXT    NOT NULL,
    version_no     INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_user_permissions_user_id ON user_permissions(user_id);
CREATE INDEX IF NOT EXISTS idx_user_permissions_key ON user_permissions(permission_key);
"""


async def init_schema(conn: aiosqlite.Connection) -> None:
    """在给定连接上创建 IAM 表（``CREATE TABLE IF NOT EXISTS``，幂等）。

    正式部署由 ``migrations/`` 顺序迁移负责；本函数供测试与开发期首次启动使用。

    实现说明：使用逐条 ``execute`` 而非 ``executescript``。``executescript`` 会
    在执行前隐式 COMMIT 当前事务（Python sqlite3 文档行为），这会破坏
    ``Database.write_connection`` 的 ``BEGIN IMMEDIATE``/``COMMIT`` 事务边界，
    导致上下文管理器退出时 ``COMMIT`` 报 ``no transaction is active``。
    """
    for raw in SCHEMA_SQL.split(";"):
        stmt = raw.strip()
        if stmt:
            await conn.execute(stmt)


# --------------------------------------------------------------------------- #
# 行映射 dataclass
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UserRecord:
    """``users`` 表行映射，供 service 层内部使用。

    ``password_hash`` 是 bcrypt 哈希字符串；明文密码绝不进入本结构、绝不持久化。
    """

    id: str
    username: str
    display_name: str
    password_hash: str
    status: str
    email: str | None = None
    last_login_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
    version_no: int = 1


@dataclass(frozen=True)
class SessionRecord:
    """``sessions`` 表行映射。``token_hash`` 为 SHA-256，明文 token 不入库。"""

    id: str
    user_id: str
    token_hash: str
    created_at: str
    expires_at: str
    revoked_at: str | None = None
    last_used_at: str | None = None
    user_agent: str | None = None

    @property
    def is_revoked(self) -> bool:
        """会话是否已撤销（``revoked_at`` 非空）。"""
        return self.revoked_at is not None


@dataclass
class SessionInsert:
    """新建会话时传给 ``create_session`` 的输入。明文 token 不进入本结构。"""

    id: str
    user_id: str
    token_hash: str
    created_at: str
    expires_at: str
    user_agent: str | None = field(default=None)


@dataclass(frozen=True)
class SettingRecord:
    """``system_settings`` 表行映射（TASK-032）。

    安全约束：
    - 敏感设置（``is_secret=True``）的 ``value`` 列始终为 ``None``；明文绝不进入本结构。
    - ``secret_id`` 为 ``SecretService`` 返回的 opaque 引用；``fingerprint`` 为不可逆指纹。
    - 非敏感设置的 ``value`` 为 JSON 序列化字符串，由 service 层反序列化。
    """

    key: str
    value: str | None
    value_type: str
    is_secret: bool
    secret_id: str | None
    fingerprint: str | None
    updated_at: str
    updated_by: str
    version_no: int = 1


# --------------------------------------------------------------------------- #
# Protocol（保留原有接口契约）
# --------------------------------------------------------------------------- #


class IamRepository(Protocol):
    async def get_user_by_id(self, user_id: str) -> UserView | None:
        """按 ID 查用户；不存在返回 None，不抛 HTTP 异常。"""
        ...

    async def get_user_auth_record(self, username: str) -> dict | None:
        """返回仅供登录使用的密码哈希记录；调用者不得将其映射到 API。"""
        ...

    async def list_users(self, query: UserQuery) -> UserPage:
        """使用稳定排序和不透明游标查询用户。"""
        ...

    async def save_user(self, user: UserView, expected_version: int | None) -> UserView:
        """新增或乐观锁更新用户；冲突时不得覆盖较新数据。"""
        ...

    async def get_setting(self, key: str) -> SettingView | None:
        """按稳定 key 读取非敏感设置视图；未知 key 返回 None。"""
        ...

    async def save_setting(self, setting: SettingView, expected_version: int | None) -> SettingView:
        """创建或乐观锁更新设置并返回新版本；不得保存 Secret 明文。"""
        ...


# --------------------------------------------------------------------------- #
# SQLite 具体实现
# --------------------------------------------------------------------------- #


def _row_to_user(row: aiosqlite.Row | tuple) -> UserRecord:
    """把 ``users`` 表行映射为 ``UserRecord``。"""
    if isinstance(row, aiosqlite.Row):
        row = tuple(row)
    return UserRecord(
        id=str(row[0]),
        username=str(row[1]),
        display_name=str(row[2]),
        password_hash=str(row[3]),
        email=row[4] if row[4] is not None else None,
        status=str(row[5]),
        last_login_at=row[6] if row[6] is not None else None,
        created_at=str(row[7]),
        updated_at=str(row[8]),
        version_no=int(row[9]),
    )


def _row_to_session(row: aiosqlite.Row | tuple) -> SessionRecord:
    """把 ``sessions`` 表行映射为 ``SessionRecord``。"""
    if isinstance(row, aiosqlite.Row):
        row = tuple(row)
    return SessionRecord(
        id=str(row[0]),
        user_id=str(row[1]),
        token_hash=str(row[2]),
        created_at=str(row[3]),
        expires_at=str(row[4]),
        revoked_at=row[5] if row[5] is not None else None,
        last_used_at=row[6] if row[6] is not None else None,
        user_agent=row[7] if row[7] is not None else None,
    )


_USER_COLUMNS: str = (
    "id, username, display_name, password_hash, email, status, "
    "last_login_at, created_at, updated_at, version_no"
)

_SESSION_COLUMNS: str = (
    "id, user_id, token_hash, created_at, expires_at, revoked_at, "
    "last_used_at, user_agent"
)


class SqliteIamRepository:
    """``users`` 与 ``sessions`` 表的 SQLite 仓储实现。

    所有方法接受 ``aiosqlite.Connection``（由 ``SqliteUnitOfWork`` 提供），
    不自开事务、不调用网络、不写日志。明文密码与明文 token 绝不进入本类任何方法
    的输入或输出；本类只处理已哈希的 ``password_hash`` 与 ``token_hash``。

    谁调用它：
        ``IamServiceImpl`` 在 ``SqliteUnitOfWork`` 事务内调用本类方法读写数据。

    安全约束：
        - ``get_user_auth_record`` 返回的 dict 含 ``password_hash``，仅供登录
          恒定时间验证使用；调用方不得将其映射到 API 响应或日志；
        - ``create_session`` 接受 ``SessionInsert.token_hash``（已哈希），明文
          token 不入库；
        - 所有方法不记录 SQL 参数，避免密码哈希或 token 哈希进入日志。
    """

    async def get_user_by_username(
        self, conn: aiosqlite.Connection, username: str
    ) -> UserRecord | None:
        """按 username 查询用户；不存在返回 None。username 大小写敏感。"""
        sql = f"SELECT {_USER_COLUMNS} FROM users WHERE username = ? LIMIT 1"
        async with conn.execute(sql, (username,)) as cur:
            row = await cur.fetchone()
        return _row_to_user(row) if row is not None else None

    async def get_user_by_id(
        self, conn: aiosqlite.Connection, user_id: str
    ) -> UserRecord | None:
        """按 id 查询用户；不存在返回 None。"""
        sql = f"SELECT {_USER_COLUMNS} FROM users WHERE id = ? LIMIT 1"
        async with conn.execute(sql, (user_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_user(row) if row is not None else None

    async def get_user_auth_record(
        self, conn: aiosqlite.Connection, username: str
    ) -> dict | None:
        """返回仅供登录使用的字典记录（含 ``password_hash``、``status``、``id``）。

        调用方不得将返回值映射到 API 或日志。不存在返回 None。
        对应 ``IamRepository.get_user_auth_record`` Protocol。
        """
        user = await self.get_user_by_username(conn, username)
        if user is None:
            return None
        return {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "password_hash": user.password_hash,
            "status": user.status,
            "version_no": user.version_no,
        }

    async def update_last_login(
        self,
        conn: aiosqlite.Connection,
        user_id: str,
        at: datetime,
    ) -> None:
        """更新 ``last_login_at`` 与 ``updated_at``；不影响 ``version_no``。

        登录不算用户资料修改，不递增版本号，避免触发乐观锁冲突。
        ``at`` 由调用方传入带时区 datetime，序列化为 ISO 8601 字符串。
        """
        iso = at.astimezone().isoformat() if at.tzinfo is None else at.isoformat()
        await conn.execute(
            "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
            (iso, iso, user_id),
        )

    async def create_session(
        self,
        conn: aiosqlite.Connection,
        session: SessionInsert,
    ) -> None:
        """插入新会话行。``session.token_hash`` 必须已哈希，明文 token 不入库。"""
        await conn.execute(
            "INSERT INTO sessions (id, user_id, token_hash, created_at, "
            "expires_at, revoked_at, last_used_at, user_agent) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                session.id,
                session.user_id,
                session.token_hash,
                session.created_at,
                session.expires_at,
                session.user_agent,
            ),
        )

    async def get_session_by_token_hash(
        self,
        conn: aiosqlite.Connection,
        token_hash: str,
    ) -> SessionRecord | None:
        """按 ``token_hash`` 查会话；不存在返回 None。供认证中间件验证 token。"""
        sql = f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE token_hash = ? LIMIT 1"
        async with conn.execute(sql, (token_hash,)) as cur:
            row = await cur.fetchone()
        return _row_to_session(row) if row is not None else None

    async def get_session_by_id(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
    ) -> SessionRecord | None:
        """按 ``id`` 查会话；不存在返回 None。供 ``logout`` 定位会话。"""
        sql = f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id = ? LIMIT 1"
        async with conn.execute(sql, (session_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_session(row) if row is not None else None

    async def revoke_session(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
        at: datetime,
    ) -> bool:
        """撤销指定会话（设置 ``revoked_at``）。

        - 重复撤销视为成功（幂等）：若 ``revoked_at`` 已非空，本次更新影响行数
          仍为 1（UPDATE 命中该行），返回 ``True``；
        - 会话不存在返回 ``False``，调用方据此决定是否记审计。

        :returns: 是否命中一行（存在该 session_id）。
        """
        iso = at.astimezone().isoformat() if at.tzinfo is None else at.isoformat()
        cursor = await conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (iso, session_id),
        )
        try:
            rowcount = cursor.rowcount
        finally:
            await cursor.close()

        if rowcount > 0:
            return True

        # 幂等：session 存在但已被撤销，返回 True；不存在返回 False
        existing = await self.get_session_by_id(conn, session_id)
        return existing is not None

    async def revoke_sessions_for_user(
        self,
        conn: aiosqlite.Connection,
        user_id: str,
        at: datetime,
    ) -> int:
        """撤销某用户全部未撤销会话；返回本次新撤销的行数。

        用于禁用用户时使其现有会话立即失效（TASK-030 验收"禁用用户的会话不可继续使用"）。
        """
        iso = at.astimezone().isoformat() if at.tzinfo is None else at.isoformat()
        cursor = await conn.execute(
            "UPDATE sessions SET revoked_at = ? "
            "WHERE user_id = ? AND revoked_at IS NULL",
            (iso, user_id),
        )
        try:
            rowcount = cursor.rowcount
        finally:
            await cursor.close()
        return rowcount

    async def touch_session_last_used(
        self,
        conn: aiosqlite.Connection,
        session_id: str,
        at: datetime,
    ) -> None:
        """更新会话 ``last_used_at``；不影响有效性，仅供审计与清理。"""
        iso = at.astimezone().isoformat() if at.tzinfo is None else at.isoformat()
        await conn.execute(
            "UPDATE sessions SET last_used_at = ? WHERE id = ?",
            (iso, session_id),
        )

    # ------------------------------------------------------------------ #
    # 用户权限（user_permissions 表）—— TASK-031
    # ------------------------------------------------------------------ #

    async def get_user_permissions(
        self,
        conn: aiosqlite.Connection,
        user_id: str,
    ) -> list[str]:
        """返回指定用户的全部 permission_key（角色列表），按字母序排序保证稳定。

        不存在或无权限返回空列表。
        """
        sql = (
            "SELECT permission_key FROM user_permissions "
            "WHERE user_id = ? ORDER BY permission_key"
        )
        async with conn.execute(sql, (user_id,)) as cur:
            rows = await cur.fetchall()
        return [str(r[0]) for r in rows]

    async def replace_user_permissions(
        self,
        conn: aiosqlite.Connection,
        user_id: str,
        permission_keys: list[str],
        at: datetime,
    ) -> None:
        """全量替换用户权限：先删除旧行再插入新行。

        调用方应在同一事务内调用，避免中间状态可见。``permission_keys``
        应已通过 ``validate_permission_keys`` 校验去重。
        """
        iso = at.astimezone().isoformat() if at.tzinfo is None else at.isoformat()
        await conn.execute(
            "DELETE FROM user_permissions WHERE user_id = ?", (user_id,)
        )
        for key in permission_keys:
            perm_id = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO user_permissions (id, user_id, permission_key, "
                "created_at) VALUES (?, ?, ?, ?)",
                (perm_id, user_id, key, iso),
            )

    async def count_active_users_with_permission(
        self,
        conn: aiosqlite.Connection,
        permission_key: str,
    ) -> int:
        """统计拥有指定 permission_key 的 ACTIVE 用户数。

        用于"最后一个管理员不能被禁用"校验。
        """
        sql = (
            "SELECT COUNT(*) FROM user_permissions up "
            "JOIN users u ON u.id = up.user_id "
            "WHERE up.permission_key = ? AND u.status = 'ACTIVE'"
        )
        async with conn.execute(sql, (permission_key,)) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row is not None else 0

    # ------------------------------------------------------------------ #
    # 用户 CRUD —— TASK-031
    # ------------------------------------------------------------------ #

    async def create_user_record(
        self,
        conn: aiosqlite.Connection,
        *,
        user_id: str,
        username: str,
        display_name: str,
        password_hash: str,
        status: str,
        at: datetime,
    ) -> None:
        """插入一行 users 记录。``password_hash`` 必须已哈希。"""
        iso = at.astimezone().isoformat() if at.tzinfo is None else at.isoformat()
        await conn.execute(
            "INSERT INTO users (id, username, display_name, password_hash, "
            "email, status, last_login_at, created_at, updated_at, version_no) "
            "VALUES (?, ?, ?, ?, NULL, ?, NULL, ?, ?, 1)",
            (user_id, username, display_name, password_hash, status, iso, iso),
        )

    async def update_user_fields(
        self,
        conn: aiosqlite.Connection,
        user_id: str,
        *,
        display_name: str | None = None,
        status: str | None = None,
        at: datetime,
    ) -> int:
        """部分更新用户字段并递增 ``version_no``；返回新 version_no。

        仅更新非 None 字段。``updated_at`` 始终更新。若行不存在返回 0。
        """
        iso = at.astimezone().isoformat() if at.tzinfo is None else at.isoformat()
        sets: list[str] = ["updated_at = ?", "version_no = version_no + 1"]
        params: list[object] = [iso]
        if display_name is not None:
            sets.append("display_name = ?")
            params.append(display_name)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        params.append(user_id)
        sql = f"UPDATE users SET {', '.join(sets)} WHERE id = ?"
        cursor = await conn.execute(sql, tuple(params))
        try:
            rowcount = cursor.rowcount
        finally:
            await cursor.close()
        if rowcount == 0:
            return 0
        async with conn.execute(
            "SELECT version_no FROM users WHERE id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row is not None else 0

    async def list_users_page(
        self,
        conn: aiosqlite.Connection,
        *,
        cursor: str | None = None,
        limit: int = 50,
        status_filter: str | None = None,
        keyword: str | None = None,
    ) -> tuple[list[UserRecord], str | None]:
        """游标分页查询用户列表，返回 (记录列表, 下一页游标)。

        游标为最后一条记录的 ``id``（稳定排序 ``ORDER BY id``）。
        ``limit`` 上限 200，默认 50。``status_filter`` 为 ACTIVE/DISABLED。
        ``keyword`` 模糊匹配 username 或 display_name。
        """
        effective_limit = max(1, min(limit, 200))
        conditions: list[str] = []
        params: list[object] = []
        if cursor:
            conditions.append("id > ?")
            params.append(cursor)
        if status_filter:
            conditions.append("status = ?")
            params.append(status_filter)
        if keyword:
            conditions.append("(username LIKE ? OR display_name LIKE ?)")
            params.append(f"%{keyword}%")
            params.append(f"%{keyword}%")
        where_clause = (
            " WHERE " + " AND ".join(conditions) if conditions else ""
        )
        sql = (
            f"SELECT {_USER_COLUMNS} FROM users{where_clause} "
            f"ORDER BY id LIMIT ?"
        )
        params.append(effective_limit + 1)
        async with conn.execute(sql, tuple(params)) as cur:
            rows = list(await cur.fetchall())
        has_more = len(rows) > effective_limit
        page_rows = rows[:effective_limit]
        records = [_row_to_user(r) for r in page_rows]
        next_cursor = records[-1].id if has_more and records else None
        return records, next_cursor

    # ------------------------------------------------------------------ #
    # 系统设置 CRUD —— TASK-032
    # ------------------------------------------------------------------ #

    async def get_setting(
        self,
        conn: aiosqlite.Connection,
        key: str,
    ) -> SettingRecord | None:
        """按 key 读取系统设置；不存在返回 ``None``。

        安全约束：敏感设置的 ``value`` 列在写入时即为 ``None``，本方法原样返回；
        明文绝不进入本结构或日志。
        """
        sql = (
            "SELECT key, value, value_type, is_secret, secret_id, fingerprint, "
            "updated_at, updated_by, version_no "
            "FROM system_settings WHERE key = ? LIMIT 1"
        )
        async with conn.execute(sql, (key,)) as cur:
            row = await cur.fetchone()
        return _row_to_setting(row) if row is not None else None

    async def insert_setting(
        self,
        conn: aiosqlite.Connection,
        *,
        key: str,
        value: str | None,
        value_type: str,
        is_secret: bool,
        secret_id: str | None,
        fingerprint: str | None,
        updated_at: str,
        updated_by: str,
    ) -> None:
        """插入一行 system_settings；version_no 从 1 开始。

        调用方应先 ``get_setting`` 确认不存在再调用本方法。重复 key 会触发
        ``IntegrityError``（PRIMARY KEY 冲突），由事务回滚。
        """
        await conn.execute(
            "INSERT INTO system_settings "
            "(key, value, value_type, is_secret, secret_id, fingerprint, "
            "updated_at, updated_by, version_no) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                key,
                value,
                value_type,
                1 if is_secret else 0,
                secret_id,
                fingerprint,
                updated_at,
                updated_by,
            ),
        )


def _row_to_setting(row: aiosqlite.Row | tuple) -> SettingRecord:
    """把 ``system_settings`` 表行映射为 ``SettingRecord``。"""
    if isinstance(row, aiosqlite.Row):
        row = tuple(row)
    return SettingRecord(
        key=str(row[0]),
        value=row[1] if row[1] is not None else None,
        value_type=str(row[2]),
        is_secret=bool(row[3]),
        secret_id=row[4] if row[4] is not None else None,
        fingerprint=row[5] if row[5] is not None else None,
        updated_at=str(row[6]),
        updated_by=str(row[7]),
        version_no=int(row[8]),
    )


def new_session_id() -> str:
    """生成新会话 ID（UUID v4 字符串）。"""
    return str(uuid.uuid4())


def new_user_id() -> str:
    """生成新用户 ID（UUID v4 字符串）。"""
    return str(uuid.uuid4())


__all__ = [
    "SCHEMA_SQL",
    "IamRepository",
    "SessionInsert",
    "SessionRecord",
    "SettingRecord",
    "SqliteIamRepository",
    "UserRecord",
    "init_schema",
    "new_session_id",
    "new_user_id",
]
