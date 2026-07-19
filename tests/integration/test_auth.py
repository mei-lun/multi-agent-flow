"""TASK-030 集成测试：本地登录与会话。

验收标准：
1. 本地用户登录成功返回 session token。
2. 登出后 session 立即失效（``revoked_at`` 非空，重复 logout 幂等）。
3. 密码哈希存储（不存明文）：``users.password_hash`` 为 bcrypt 哈希形态。
4. session token 安全：``sessions.token_hash`` 为 SHA-256 hex，明文 token 不入库。
5. 错误密码拒绝；用户不存在与密码错误返回相同错误码。
6. 禁用用户的会话不可继续使用：``revoke_sessions_for_user`` 撤销全部未撤销会话；
   ``login`` 拒绝 DISABLED 用户。
7. 密码和会话 Token 不写日志（通过代码审查与日志断言覆盖）。

测试范围：
- ``apps/server/src/maf_server/core/security.py``：密码哈希、token 生成与验证。
- ``apps/server/src/maf_server/modules/iam/{schemas,repository,service,router}.py``。
- 不测试 RBAC（TASK-031 范围）、不测试 OIDC（不在 TASK-030 范围）。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from maf_contracts.common import ActorContext
from maf_domain.errors import ErrorCode, UnauthenticatedError
from maf_server.api.errors import register_error_handlers
from maf_server.config import ServerSettings
from maf_server.core.database import Database
from maf_server.core.security import (
    generate_session_token,
    hash_password,
    hash_session_token,
    is_password_hash,
    verify_password,
    verify_session_token,
)
from maf_server.modules.iam.repository import (
    SessionRecord,
    SqliteIamRepository,
    init_schema,
)
from maf_server.modules.iam.router import SESSION_COOKIE_NAME, build_auth_router
from maf_server.modules.iam.service import IamServiceImpl, seed_local_user

_SECRET_PLAINTEXT = "test-secret-for-auth-task-030"
_TEST_PASSWORD = "correct-horse-battery-staple-030"
_TEST_PASSWORD_WRONG = "wrong-password-030"
_TEST_USERNAME = "alice"
_TEST_DISPLAY_NAME = "Alice Task030"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_maf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove any ``MAF_*`` env vars so tests start from a clean slate."""
    for key in list(os.environ):
        if key.startswith("MAF_"):
            monkeypatch.delenv(key, raising=False)


def _make_settings(tmp_path: Path, **overrides: object) -> ServerSettings:
    """构建测试用 ServerSettings，数据库路径落在 ``tmp_path`` 下。"""
    kwargs: dict[str, object] = dict(
        organization_id="org-001",
        business_db_path=Path("maf.db"),
        checkpointer_db_path=Path("checkpoints.db"),
        artifact_root=Path("artifacts"),
        workspace_root=Path("workspaces"),
        git_repo_root=tmp_path / "repo",
        public_base_url="http://localhost:8000",
        secret_key=_SECRET_PLAINTEXT,
        data_dir=tmp_path,
        _env_file=None,
    )
    kwargs.update(overrides)
    return ServerSettings(**kwargs)


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    """已初始化并建好 IAM 表的 Database，测试结束自动关闭。"""
    settings = _make_settings(tmp_path)
    database = Database(settings)
    await database.initialize()
    # 在 business 库上创建 IAM 表（CREATE TABLE IF NOT EXISTS，幂等）。
    # 使用 init_schema（逐条 execute）而非 executescript：后者会隐式 COMMIT，
    # 破坏 write_connection 的 BEGIN IMMEDIATE/COMMIT 事务边界。
    async with database.write_connection() as conn:
        await init_schema(conn)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def seeded_db(db: Database) -> tuple[Database, str]:
    """已初始化 + 建表 + 种子一个 ACTIVE 用户的 Database；返回 (db, user_id)。"""
    user_id = await seed_local_user(
        db,
        username=_TEST_USERNAME,
        display_name=_TEST_DISPLAY_NAME,
        password_plain=_TEST_PASSWORD,
    )
    return db, user_id


def _actor(user_id: str) -> ActorContext:
    """构造测试用 ActorContext。"""
    return ActorContext(
        user_id=user_id,
        organization_id="org-001",
        permission_keys=[],
        trace_id="test-trace-030",
    )


# --------------------------------------------------------------------------- #
# 验收：密码哈希原语（core/security.py）
# --------------------------------------------------------------------------- #


class TestPasswordHashing:
    """密码哈希原语：bcrypt、恒定时间验证、不存明文。"""

    def test_hash_password_returns_bcrypt_format(self) -> None:
        h = hash_password(_TEST_PASSWORD)
        assert isinstance(h, str)
        assert is_password_hash(h), "哈希应为 bcrypt 形态"
        assert h.startswith(("$2a$", "$2b$", "$2y$"))

    def test_hash_password_does_not_contain_plaintext(self) -> None:
        """密码哈希不应包含明文密码片段。"""
        h = hash_password(_TEST_PASSWORD)
        assert _TEST_PASSWORD not in h

    def test_verify_password_success(self) -> None:
        h = hash_password(_TEST_PASSWORD)
        assert verify_password(_TEST_PASSWORD, h) is True

    def test_verify_password_wrong(self) -> None:
        h = hash_password(_TEST_PASSWORD)
        assert verify_password(_TEST_PASSWORD_WRONG, h) is False

    def test_verify_password_empty(self) -> None:
        """空密码或空哈希返回 False，不抛异常。"""
        assert verify_password("", "anything") is False
        assert verify_password(_TEST_PASSWORD, "") is False
        assert verify_password(_TEST_PASSWORD, None) is False  # type: ignore[arg-type]

    def test_hash_password_unique_per_call(self) -> None:
        """同一明文每次哈希结果不同（bcrypt 自带 salt）。"""
        h1 = hash_password(_TEST_PASSWORD)
        h2 = hash_password(_TEST_PASSWORD)
        assert h1 != h2
        # 但两者都能验证通过
        assert verify_password(_TEST_PASSWORD, h1)
        assert verify_password(_TEST_PASSWORD, h2)

    def test_hash_password_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="不能为空"):
            hash_password("")

    def test_verify_password_constant_time_on_invalid_hash(self) -> None:
        """格式异常的哈希返回 False 而非抛错，避免泄露。"""
        assert verify_password(_TEST_PASSWORD, "not-a-hash") is False
        assert verify_password(_TEST_PASSWORD, "$2b$invalid") is False


# --------------------------------------------------------------------------- #
# 验收：Session Token 原语
# --------------------------------------------------------------------------- #


class TestSessionToken:
    """Session Token 生成、哈希与恒定时间验证。"""

    def test_generate_token_is_urlsafe_non_empty(self) -> None:
        t = generate_session_token()
        assert isinstance(t, str)
        assert len(t) >= 32
        # URL-safe 字符集
        for c in t:
            assert c.isalnum() or c in "-_"

    def test_generate_token_unique(self) -> None:
        tokens = {generate_session_token() for _ in range(64)}
        assert len(tokens) == 64, "token 应几乎不碰撞"

    def test_hash_token_is_sha256_hex(self) -> None:
        t = generate_session_token()
        h = hash_session_token(t)
        assert isinstance(h, str)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_token_does_not_contain_plaintext(self) -> None:
        """token 哈希不应包含明文 token 片段。"""
        t = generate_session_token()
        h = hash_session_token(t)
        assert t not in h

    def test_verify_token_success(self) -> None:
        t = generate_session_token()
        h = hash_session_token(t)
        assert verify_session_token(t, h) is True

    def test_verify_token_wrong(self) -> None:
        t = generate_session_token()
        h = hash_session_token(t)
        assert verify_session_token("other-token", h) is False

    def test_verify_token_empty(self) -> None:
        """空 token 或空哈希返回 False，不抛异常。"""
        assert verify_session_token("", "hash") is False
        assert verify_session_token("token", "") is False

    def test_hash_token_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="不能为空"):
            hash_session_token("")


# --------------------------------------------------------------------------- #
# 验收 1：本地用户登录成功返回 session token
# --------------------------------------------------------------------------- #


class TestLoginSuccess:
    """登录成功路径：返回 token、用户视图，更新 last_login_at。"""

    @pytest.mark.asyncio
    async def test_login_returns_session_with_token(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        db, user_id = seeded_db
        service = IamServiceImpl(db)
        session = await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )

        assert "session_id" in session
        assert "expires_at" in session
        assert "token" in session
        assert isinstance(session["token"], str)
        assert len(session["token"]) >= 32
        assert session["user"]["id"] == user_id
        assert session["user"]["username"] == _TEST_USERNAME
        assert session["user"]["display_name"] == _TEST_DISPLAY_NAME
        assert session["user"]["status"] == "ACTIVE"
        assert session["user"]["permissions"] == []

    @pytest.mark.asyncio
    async def test_login_updates_last_login_at(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        db, user_id = seeded_db
        service = IamServiceImpl(db)
        # 登录前 last_login_at 为 NULL
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT last_login_at FROM users WHERE id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] is None

        await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )

        # 登录后 last_login_at 应为 ISO 8601 字符串
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT last_login_at FROM users WHERE id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] is not None
            assert "T" in row[0]

    @pytest.mark.asyncio
    async def test_login_creates_session_row_with_hashed_token(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        """登录后 sessions 表应有一行，token_hash 为 SHA-256，明文 token 不入库。"""
        db, user_id = seeded_db
        service = IamServiceImpl(db)
        session = await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )

        token_plain = session["token"]
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT id, user_id, token_hash, revoked_at FROM sessions "
                "WHERE id = ?",
                (session["session_id"],),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == session["session_id"]
            assert row[1] == user_id
            # token_hash 是 SHA-256 hex
            assert len(row[2]) == 64
            assert all(c in "0123456789abcdef" for c in row[2])
            # 明文 token 不入库
            assert token_plain not in row[2]
            assert row[2] == hash_session_token(token_plain)
            # revoked_at 为 NULL
            assert row[3] is None

    @pytest.mark.asyncio
    async def test_multiple_logins_create_distinct_sessions(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        """多次登录创建多个独立 session，token 互不相同。"""
        db, _ = seeded_db
        service = IamServiceImpl(db)
        s1 = await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )
        s2 = await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )
        assert s1["session_id"] != s2["session_id"]
        assert s1["token"] != s2["token"]


# --------------------------------------------------------------------------- #
# 验收 5：错误密码拒绝；用户不存在与密码错误返回相同错误
# --------------------------------------------------------------------------- #


class TestLoginFailure:
    """登录失败路径：错误密码、用户不存在、禁用用户。"""

    @pytest.mark.asyncio
    async def test_wrong_password_rejected(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        db, _ = seeded_db
        service = IamServiceImpl(db)
        with pytest.raises(UnauthenticatedError) as exc_info:
            await service.login(
                {"username": _TEST_USERNAME, "password": _TEST_PASSWORD_WRONG}
            )
        assert exc_info.value.error_code == ErrorCode.UNAUTHENTICATED
        # 错误信息不泄露用户存在
        assert _TEST_USERNAME not in exc_info.value.message or "或" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_nonexistent_user_rejected(self, db: Database) -> None:
        """用户不存在时抛相同错误码，错误信息不区分。"""
        service = IamServiceImpl(db)
        with pytest.raises(UnauthenticatedError) as exc_info:
            await service.login(
                {"username": "ghost-user-030", "password": "any-password"}
            )
        assert exc_info.value.error_code == ErrorCode.UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_nonexistent_and_wrong_password_same_error(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        """用户不存在与密码错误返回相同错误码与信息。"""
        db, _ = seeded_db
        service = IamServiceImpl(db)

        err_nonexistent: UnauthenticatedError | None = None
        err_wrong_pw: UnauthenticatedError | None = None
        try:
            await service.login(
                {"username": "ghost-user-030", "password": "any"}
            )
        except UnauthenticatedError as e:
            err_nonexistent = e
        try:
            await service.login(
                {"username": _TEST_USERNAME, "password": _TEST_PASSWORD_WRONG}
            )
        except UnauthenticatedError as e:
            err_wrong_pw = e

        assert err_nonexistent is not None
        assert err_wrong_pw is not None
        assert err_nonexistent.error_code == err_wrong_pw.error_code
        assert err_nonexistent.message == err_wrong_pw.message

    @pytest.mark.asyncio
    async def test_disabled_user_login_rejected(self, db: Database) -> None:
        """DISABLED 用户登录被拒绝，错误码与密码错误一致。"""
        user_id = await seed_local_user(
            db,
            username="bob-disabled",
            display_name="Bob",
            password_plain=_TEST_PASSWORD,
            status="DISABLED",
        )
        assert user_id
        service = IamServiceImpl(db)
        with pytest.raises(UnauthenticatedError) as exc_info:
            await service.login(
                {"username": "bob-disabled", "password": _TEST_PASSWORD}
            )
        assert exc_info.value.error_code == ErrorCode.UNAUTHENTICATED

    @pytest.mark.asyncio
    async def test_empty_username_rejected(self, seeded_db: tuple[Database, str]) -> None:
        db, _ = seeded_db
        service = IamServiceImpl(db)
        with pytest.raises(UnauthenticatedError):
            await service.login({"username": "", "password": _TEST_PASSWORD})

    @pytest.mark.asyncio
    async def test_empty_password_rejected(self, seeded_db: tuple[Database, str]) -> None:
        db, _ = seeded_db
        service = IamServiceImpl(db)
        with pytest.raises(UnauthenticatedError):
            await service.login({"username": _TEST_USERNAME, "password": ""})

    @pytest.mark.asyncio
    async def test_username_trimmed(self, seeded_db: tuple[Database, str]) -> None:
        """用户名首尾空白应被去除，仍能登录成功。"""
        db, _ = seeded_db
        service = IamServiceImpl(db)
        session = await service.login(
            {"username": f"  {_TEST_USERNAME}  ", "password": _TEST_PASSWORD}
        )
        assert session["user"]["username"] == _TEST_USERNAME


# --------------------------------------------------------------------------- #
# 验收 2：登出后 session 立即失效
# --------------------------------------------------------------------------- #


class TestLogout:
    """登出路径：session 撤销、幂等、归属校验。"""

    @pytest.mark.asyncio
    async def test_logout_sets_revoked_at(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        db, user_id = seeded_db
        service = IamServiceImpl(db)
        session = await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )

        # 登出前 revoked_at 为 NULL
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT revoked_at FROM sessions WHERE id = ?",
                (session["session_id"],),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] is None

        await service.logout(_actor(user_id), session["session_id"])

        # 登出后 revoked_at 非空
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT revoked_at FROM sessions WHERE id = ?",
                (session["session_id"],),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] is not None

    @pytest.mark.asyncio
    async def test_logout_idempotent(self, seeded_db: tuple[Database, str]) -> None:
        """重复注销不抛错，视为成功。"""
        db, user_id = seeded_db
        service = IamServiceImpl(db)
        session = await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )
        await service.logout(_actor(user_id), session["session_id"])
        # 重复注销不抛错
        await service.logout(_actor(user_id), session["session_id"])
        await service.logout(_actor(user_id), session["session_id"])

    @pytest.mark.asyncio
    async def test_logout_nonexistent_session_idempotent(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        """注销不存在的 session 视为已注销，不抛错。"""
        db, user_id = seeded_db
        service = IamServiceImpl(db)
        await service.logout(_actor(user_id), "nonexistent-session-id")

    @pytest.mark.asyncio
    async def test_logout_rejects_cross_user(
        self, db: Database
    ) -> None:
        """用户 A 不能撤销用户 B 的 session。"""
        await seed_local_user(
            db, username="user-a", display_name="A", password_plain=_TEST_PASSWORD
        )
        user_b = await seed_local_user(
            db, username="user-b", display_name="B", password_plain=_TEST_PASSWORD
        )
        service = IamServiceImpl(db)

        # A 登录拿到 session
        session_a = await service.login(
            {"username": "user-a", "password": _TEST_PASSWORD}
        )
        # B 尝试撤销 A 的 session：应被拒绝
        with pytest.raises(UnauthenticatedError):
            await service.logout(_actor(user_b), session_a["session_id"])

        # A 的 session 仍未撤销
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT revoked_at FROM sessions WHERE id = ?",
                (session_a["session_id"],),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] is None

    @pytest.mark.asyncio
    async def test_logout_invalid_actor_rejected(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        """actor 缺失 user_id 时拒绝 logout。"""
        db, _ = seeded_db
        service = IamServiceImpl(db)
        bad_actor = ActorContext(
            user_id="",
            organization_id="org-001",
            permission_keys=[],
            trace_id="t",
        )
        with pytest.raises(UnauthenticatedError):
            await service.logout(bad_actor, "any-session-id")

    @pytest.mark.asyncio
    async def test_logout_after_logout_session_invalid(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        """登出后该 session 的 revoked_at 永久非空，后续请求必须视为未认证。

        本测试通过查 sessions 表验证 revoked_at 状态；认证中间件（后续任务）
        应据此拒绝该 session 的后续请求。
        """
        db, user_id = seeded_db
        service = IamServiceImpl(db)
        session = await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )
        await service.logout(_actor(user_id), session["session_id"])

        # 再次查 sessions 表，revoked_at 仍非空
        repo = SqliteIamRepository()
        async with db.read_connection() as conn:
            srec = await repo.get_session_by_id(conn, session["session_id"])
        assert srec is not None
        assert srec.is_revoked is True
        assert srec.revoked_at is not None


# --------------------------------------------------------------------------- #
# 验收 3 & 4：密码哈希不存明文；session token 不存明文
# --------------------------------------------------------------------------- #


class TestNoPlaintextStored:
    """验证密码与 token 明文都不入库。"""

    @pytest.mark.asyncio
    async def test_password_hash_not_plaintext_in_db(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        """users.password_hash 列应为 bcrypt 哈希，不含明文密码。"""
        db, _ = seeded_db
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT password_hash FROM users WHERE username = ?",
                (_TEST_USERNAME,),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            pw_hash = row[0]
        assert is_password_hash(pw_hash), "password_hash 应为 bcrypt 哈希"
        assert _TEST_PASSWORD not in pw_hash, "明文密码不应出现在哈希中"
        # 验证哈希可正确校验
        assert verify_password(_TEST_PASSWORD, pw_hash)

    @pytest.mark.asyncio
    async def test_session_token_not_plaintext_in_db(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        """sessions.token_hash 列应为 SHA-256 hex，不含明文 token。"""
        db, _ = seeded_db
        service = IamServiceImpl(db)
        session = await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )
        token_plain = session["token"]

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT token_hash FROM sessions WHERE id = ?",
                (session["session_id"],),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            token_hash = row[0]

        assert len(token_hash) == 64
        assert all(c in "0123456789abcdef" for c in token_hash)
        assert token_plain not in token_hash, "明文 token 不应入库"
        assert token_hash == hash_session_token(token_plain)

    @pytest.mark.asyncio
    async def test_no_plaintext_password_in_any_column(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        """扫描 users 与 sessions 全表，确保明文密码不出现在任何列。"""
        db, _ = seeded_db
        # 触发登录，确保有 session 行
        service = IamServiceImpl(db)
        await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )

        async with db.read_connection() as conn:
            async with conn.execute("SELECT * FROM users") as cur:
                user_rows = await cur.fetchall()
            async with conn.execute("SELECT * FROM sessions") as cur:
                session_rows = await cur.fetchall()

        for row in user_rows:
            for value in row:
                if isinstance(value, str):
                    assert _TEST_PASSWORD not in value, (
                        f"明文密码出现在 users 表：{value!r}"
                    )
        for row in session_rows:
            for value in row:
                if isinstance(value, str):
                    assert _TEST_PASSWORD not in value


# --------------------------------------------------------------------------- #
# 验收 6：禁用用户的会话不可继续使用
# --------------------------------------------------------------------------- #


class TestDisabledUserSession:
    """禁用用户时其现有会话应被撤销，且 DISABLED 用户不能登录。"""

    @pytest.mark.asyncio
    async def test_revoke_sessions_for_user_revokes_all(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        """``revoke_sessions_for_user`` 撤销该用户全部未撤销会话。"""
        db, user_id = seeded_db
        service = IamServiceImpl(db)
        # 创建 3 个会话
        s1 = await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )
        s2 = await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )
        s3 = await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )

        repo = SqliteIamRepository()
        now = datetime.now(timezone.utc)
        async with db.write_connection() as conn:
            count = await repo.revoke_sessions_for_user(conn, user_id, now)

        assert count == 3, f"应撤销 3 个会话，实际 {count}"

        # 验证三个会话 revoked_at 均非空
        async with db.read_connection() as conn:
            for sid in (s1["session_id"], s2["session_id"], s3["session_id"]):
                srec = await repo.get_session_by_id(conn, sid)
                assert srec is not None
                assert srec.is_revoked, f"session {sid} 应已撤销"

    @pytest.mark.asyncio
    async def test_revoke_sessions_idempotent(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        """已撤销的会话不再计入 revoke_sessions_for_user 返回值。"""
        db, user_id = seeded_db
        service = IamServiceImpl(db)
        await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )
        await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )

        repo = SqliteIamRepository()
        now = datetime.now(timezone.utc)
        async with db.write_connection() as conn:
            first = await repo.revoke_sessions_for_user(conn, user_id, now)
            second = await repo.revoke_sessions_for_user(conn, user_id, now)
        assert first == 2
        assert second == 0, "已撤销的会话不应再次计入"

    @pytest.mark.asyncio
    async def test_disabled_user_cannot_login(self, db: Database) -> None:
        """DISABLED 用户登录被拒绝。"""
        await seed_local_user(
            db,
            username="charlie-disabled",
            display_name="Charlie",
            password_plain=_TEST_PASSWORD,
            status="DISABLED",
        )
        service = IamServiceImpl(db)
        with pytest.raises(UnauthenticatedError):
            await service.login(
                {"username": "charlie-disabled", "password": _TEST_PASSWORD}
            )

    @pytest.mark.asyncio
    async def test_disabled_user_existing_sessions_revoked(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        """模拟禁用用户流程：先登录创建 session，再调用 revoke_sessions_for_user，
        验证 session 不可继续使用（revoked_at 非空）。
        """
        db, user_id = seeded_db
        service = IamServiceImpl(db)
        session = await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )

        # 模拟 update_user(status=DISABLED) 调用 revoke_sessions_for_user
        repo = SqliteIamRepository()
        now = datetime.now(timezone.utc)
        async with db.write_connection() as conn:
            await conn.execute(
                "UPDATE users SET status = 'DISABLED' WHERE id = ?", (user_id,)
            )
            await repo.revoke_sessions_for_user(conn, user_id, now)

        # 验证 session 已撤销
        async with db.read_connection() as conn:
            srec = await repo.get_session_by_id(conn, session["session_id"])
        assert srec is not None
        assert srec.is_revoked is True


# --------------------------------------------------------------------------- #
# 验收 7：密码和会话 Token 不写日志
# --------------------------------------------------------------------------- #


class TestNoSensitiveInLogs:
    """验证 service 与 repository 不记录密码与 token 明文。

    本测试通过捕获 logging 输出，断言明文密码与 token 不出现在日志中。
    """

    @pytest.mark.asyncio
    async def test_login_does_not_log_password_or_token(
        self,
        seeded_db: tuple[Database, str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        db, _ = seeded_db
        service = IamServiceImpl(db)
        import logging

        with caplog.at_level(logging.DEBUG, logger="maf_server"):
            session = await service.login(
                {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
            )
            token = session["token"]

        # 扫描所有日志记录
        for record in caplog.records:
            msg = record.getMessage()
            assert _TEST_PASSWORD not in msg, (
                f"密码出现在日志中：{msg!r}"
            )
            assert token not in msg, f"token 出现在日志中：{msg!r}"

    @pytest.mark.asyncio
    async def test_failed_login_does_not_log_password(
        self,
        seeded_db: tuple[Database, str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        db, _ = seeded_db
        service = IamServiceImpl(db)
        import logging

        with caplog.at_level(logging.DEBUG, logger="maf_server"):
            with pytest.raises(UnauthenticatedError):
                await service.login(
                    {"username": _TEST_USERNAME, "password": _TEST_PASSWORD_WRONG}
                )

        for record in caplog.records:
            msg = record.getMessage()
            assert _TEST_PASSWORD_WRONG not in msg


# --------------------------------------------------------------------------- #
# Repository 直接测试
# --------------------------------------------------------------------------- #


class TestSqliteIamRepository:
    """SqliteIamRepository 行为测试。"""

    @pytest.mark.asyncio
    async def test_get_user_by_username(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        db, user_id = seeded_db
        repo = SqliteIamRepository()
        async with db.read_connection() as conn:
            user = await repo.get_user_by_username(conn, _TEST_USERNAME)
            none_user = await repo.get_user_by_username(conn, "nonexistent")
        assert user is not None
        assert user.id == user_id
        assert user.username == _TEST_USERNAME
        assert user.status == "ACTIVE"
        assert is_password_hash(user.password_hash)
        assert none_user is None

    @pytest.mark.asyncio
    async def test_get_user_auth_record(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        db, _ = seeded_db
        repo = SqliteIamRepository()
        async with db.read_connection() as conn:
            rec = await repo.get_user_auth_record(conn, _TEST_USERNAME)
        assert rec is not None
        assert rec["username"] == _TEST_USERNAME
        assert "password_hash" in rec
        assert is_password_hash(rec["password_hash"])

    @pytest.mark.asyncio
    async def test_revoke_session_returns_false_for_nonexistent(
        self, db: Database
    ) -> None:
        """撤销不存在的 session 返回 False。"""
        repo = SqliteIamRepository()
        now = datetime.now(timezone.utc)
        async with db.write_connection() as conn:
            result = await repo.revoke_session(conn, "nonexistent", now)
        assert result is False

    @pytest.mark.asyncio
    async def test_revoke_session_idempotent(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        """重复撤销同一 session 返回 True（幂等）。"""
        db, user_id = seeded_db
        service = IamServiceImpl(db)
        session = await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )
        repo = SqliteIamRepository()
        now = datetime.now(timezone.utc)
        async with db.write_connection() as conn:
            first = await repo.revoke_session(conn, session["session_id"], now)
            second = await repo.revoke_session(conn, session["session_id"], now)
        assert first is True
        assert second is True


# --------------------------------------------------------------------------- #
# FastAPI Router 集成测试
# --------------------------------------------------------------------------- #


class TestFastAPIRouter:
    """``build_auth_router`` 暴露的 HTTP 端点测试。"""

    def _build_app(
        self, db: Database, *, secure_cookie: bool = False
    ) -> FastAPI:
        """构造挂载 auth router 的 FastAPI app，用于 TestClient。"""
        service = IamServiceImpl(db)
        app = FastAPI()
        # 注册领域错误处理器，使 UnauthenticatedError → 401 JSON 响应。
        # 生产环境由 main.py 调用；测试 app 需自行注册。
        register_error_handlers(app)
        app.include_router(
            build_auth_router(service, secure_cookie=secure_cookie)
        )
        return app

    @pytest.mark.asyncio
    async def test_login_endpoint_returns_token_and_sets_cookie(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        db, _ = seeded_db
        app = self._build_app(db, secure_cookie=False)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/auth/login",
            json={"username": _TEST_USERNAME, "password": _TEST_PASSWORD},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "token" in body
        assert len(body["token"]) >= 32
        assert body["user"]["username"] == _TEST_USERNAME
        # Cookie 应被设置
        assert SESSION_COOKIE_NAME in resp.cookies
        assert resp.cookies[SESSION_COOKIE_NAME] == body["token"]

    @pytest.mark.asyncio
    async def test_login_endpoint_wrong_password_returns_401(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        db, _ = seeded_db
        app = self._build_app(db)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/auth/login",
            json={"username": _TEST_USERNAME, "password": _TEST_PASSWORD_WRONG},
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["error"]["error_code"] == "UNAUTHENTICATED"
        # 错误信息不区分用户是否存在
        assert _TEST_USERNAME not in body["error"]["message"] or "或" in body["error"]["message"]
        # 不设 Cookie
        assert SESSION_COOKIE_NAME not in resp.cookies

    @pytest.mark.asyncio
    async def test_login_endpoint_nonexistent_user_returns_401(
        self, db: Database
    ) -> None:
        app = self._build_app(db)
        client = TestClient(app)
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "ghost", "password": "any"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_logout_endpoint_clears_cookie(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        db, user_id = seeded_db
        service = IamServiceImpl(db)
        app = self._build_app(db, secure_cookie=False)
        # 注入 stub actor 依赖
        from maf_server.modules.iam.router import _anonymous_actor_dependency

        app.dependency_overrides[_anonymous_actor_dependency] = lambda: _actor(user_id)
        client = TestClient(app)

        # 先登录拿到 session_id
        session = await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )
        sid = session["session_id"]

        resp = client.post(
            "/api/v1/auth/logout",
            json={"session_id": sid},
        )
        assert resp.status_code == 204
        # Cookie 应被清除（Set-Cookie expires）
        set_cookie_header = resp.headers.get("set-cookie", "")
        assert SESSION_COOKIE_NAME in set_cookie_header
        assert "expires=" in set_cookie_header.lower() or "max-age=0" in set_cookie_header.lower()

        # 验证 session 已撤销
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT revoked_at FROM sessions WHERE id = ?", (sid,)
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] is not None

    @pytest.mark.asyncio
    async def test_logout_endpoint_idempotent(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        db, user_id = seeded_db
        service = IamServiceImpl(db)
        app = self._build_app(db, secure_cookie=False)
        from maf_server.modules.iam.router import _anonymous_actor_dependency

        app.dependency_overrides[_anonymous_actor_dependency] = lambda: _actor(user_id)
        client = TestClient(app)

        session = await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )
        sid = session["session_id"]

        r1 = client.post("/api/v1/auth/logout", json={"session_id": sid})
        r2 = client.post("/api/v1/auth/logout", json={"session_id": sid})
        r3 = client.post("/api/v1/auth/logout", json={"session_id": sid})
        assert r1.status_code == 204
        assert r2.status_code == 204
        assert r3.status_code == 204

    @pytest.mark.asyncio
    async def test_logout_endpoint_without_actor_returns_401(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        """未注入 actor 依赖时 logout 应返回 401。"""
        db, _ = seeded_db
        app = self._build_app(db)
        # 不覆盖 _anonymous_actor_dependency，默认抛 UnauthenticatedError
        client = TestClient(app)
        resp = client.post(
            "/api/v1/auth/logout",
            json={"session_id": "any"},
        )
        assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# 会话过期测试
# --------------------------------------------------------------------------- #


class TestSessionExpiry:
    """会话过期与 TTL 配置。"""

    @pytest.mark.asyncio
    async def test_session_expiry_uses_ttl(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        """``session_ttl_seconds`` 控制会话过期时间。"""
        db, _ = seeded_db
        # TTL = 60 秒
        service = IamServiceImpl(db, session_ttl_seconds=60)
        session = await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )
        expires_at_str = session["expires_at"]
        expires_at = datetime.fromisoformat(expires_at_str)
        now = datetime.now(timezone.utc)
        delta = expires_at - now
        # 应在 60 秒左右（允许 10 秒漂移）
        assert 50 <= delta.total_seconds() <= 70

    @pytest.mark.asyncio
    async def test_session_expiry_persisted_in_db(
        self, seeded_db: tuple[Database, str]
    ) -> None:
        """sessions.expires_at 应与返回的 expires_at 一致。"""
        db, _ = seeded_db
        service = IamServiceImpl(db, session_ttl_seconds=3600)
        session = await service.login(
            {"username": _TEST_USERNAME, "password": _TEST_PASSWORD}
        )
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT expires_at FROM sessions WHERE id = ?",
                (session["session_id"],),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == session["expires_at"]
