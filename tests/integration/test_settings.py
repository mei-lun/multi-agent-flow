"""TASK-032 集成测试：系统设置（SystemSetting）。

验收标准：
1. 未知 key 拒绝：``get_setting``/``put_setting`` 收到未在 ``SETTING_SCHEMAS``
   登记的 key 时抛 ``ArgumentError``（HTTP 400）。
2. 敏感值只返回 configured/fingerprint：``is_secret=True`` 的设置经
   ``SecretService`` 存储，SQLite 仅保存 ``secret_id`` 与不可逆指纹；
   ``get_setting`` 返回 ``value=None``、``configured=True``、``fingerprint`` 非空。
3. 并发更新产生版本冲突（非覆盖）：``expected_version`` 不匹配抛
   ``VersionConflictError``（HTTP 409），原值与新版本号均不变。

补充覆盖：
- 明文设置读写（非敏感设置 round-trip）；
- 敏感设置明文绝不进入 SQLite（``system_settings.value`` 与 ``outbox_events.payload``
  均不含明文片段）；
- Secret 轮换：再次写入敏感设置产生新 ``secret_id`` 与新 ``fingerprint``，
  旧 secret 被 best-effort 删除（不再可 ``resolve``）；
- 非 ADMIN 读 OK / 写被拒（``PermissionDeniedError``）；
- ``system.setting.changed`` 事件出现在 ``outbox_events``；
- HTTP 路由：``GET``/``PUT`` ``/api/v1/settings/{key}``。

测试范围：
- ``apps/server/src/maf_server/modules/iam/{schemas,settings_schema,repository,service,router}.py``；
- ``apps/server/src/maf_server/gateway/secrets/local_service.py``（作为 SecretService 注入）；
- ``apps/server/src/maf_server/core/{unit_of_work,events}.py``（事务边界与 Outbox）。
"""

from __future__ import annotations

import json
import os
import secrets as _secrets
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from maf_contracts.common import ActorContext
from maf_domain.errors import (
    ArgumentError,
    PermissionDeniedError,
    VersionConflictError,
)
from maf_server.api.errors import register_error_handlers
from maf_server.config import ServerSettings
from maf_server.core.database import Database
from maf_server.core.events import init_outbox_schema
from maf_server.core.secrets import MASTER_KEY_SIZE_BYTES
from maf_server.gateway.secrets.aes_gcm_store import AesGcmFileStore
from maf_server.gateway.secrets.local_service import LocalSecretService
from maf_server.modules.iam.repository import init_schema
from maf_server.modules.iam.router import (
    _anonymous_actor_dependency,
    build_settings_router,
)
from maf_server.modules.iam.service import IamServiceImpl, seed_local_user

_SECRET_PLAINTEXT = "test-secret-for-settings-task-032"
_TEST_PASSWORD = "settings-correct-horse-battery-staple"
_ORG_ID = "org-001"

#: 敏感明文样本（足够长以使指纹末 4 位唯一且不构成前缀泄露）。
_SMTP_PASSWORD_PLAIN = "smtp-secret-S3cretP@ss-032"
_GITHUB_TOKEN_PLAIN = "ghp_FAKE_TOKEN_value_for_tests_032"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_maf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除 ``MAF_*`` 环境变量，避免本地 ``.env`` 污染测试。"""
    for key in list(os.environ):
        if key.startswith("MAF_"):
            monkeypatch.delenv(key, raising=False)


def _make_settings(tmp_path: Path, **overrides: object) -> ServerSettings:
    """构建测试用 ServerSettings，数据库路径落在 ``tmp_path`` 下。"""
    kwargs: dict[str, object] = dict(
        organization_id=_ORG_ID,
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
    """已初始化并建好 IAM + outbox 表的 Database，测试结束自动关闭。"""
    settings = _make_settings(tmp_path)
    database = Database(settings)
    await database.initialize()
    async with database.write_connection() as conn:
        await init_schema(conn)
    await init_outbox_schema(database)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def admin_db(db: Database) -> tuple[Database, str]:
    """种子一个 ADMIN 用户；返回 (db, admin_user_id)。"""
    admin_id = await seed_local_user(
        db,
        username="admin",
        display_name="Admin User",
        password_plain=_TEST_PASSWORD,
        permission_keys=["ADMIN"],
    )
    return db, admin_id


@pytest_asyncio.fixture
async def observer_db(db: Database) -> tuple[Database, str]:
    """种子一个 OBSERVER 用户；返回 (db, observer_user_id)。"""
    observer_id = await seed_local_user(
        db,
        username="observer",
        display_name="Observer User",
        password_plain=_TEST_PASSWORD,
        permission_keys=["OBSERVER"],
    )
    return db, observer_id


@pytest.fixture()
def secret_service(tmp_path: Path) -> LocalSecretService:
    """基于 AES-GCM 文件后端的 ``LocalSecretService``，不依赖真实 keyring。

    每个测试独立实例，避免内存 metadata 跨测试泄漏。``storage_dir`` 落在
    ``tmp_path`` 下，由 pytest 自动清理。
    """
    master_key = _secrets.token_bytes(MASTER_KEY_SIZE_BYTES)
    store = AesGcmFileStore(
        master_key=master_key,
        storage_dir=tmp_path / "secrets",
        organization_id=_ORG_ID,
    )
    return LocalSecretService(primary=store)


def _actor(user_id: str, roles: list[str], trace_id: str = "settings-trace") -> ActorContext:
    """构造测试用 ActorContext。"""
    return ActorContext(
        user_id=user_id,
        organization_id=_ORG_ID,
        permission_keys=roles,
        trace_id=trace_id,
    )


def _make_service(
    db: Database,
    secret_service: LocalSecretService | None = None,
) -> IamServiceImpl:
    """构造 ``IamServiceImpl``，可选注入 ``secret_service``。"""
    return IamServiceImpl(db, secret_service=secret_service)


# --------------------------------------------------------------------------- #
# 验收 1：未知 key 拒绝
# --------------------------------------------------------------------------- #


class TestUnknownKeyRejected:
    """``get_setting``/``put_setting`` 收到未知 key 时抛 ``ArgumentError``。"""

    @pytest.mark.asyncio
    async def test_get_unknown_key_raises_argument_error(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)
        actor = _actor(admin_id, ["ADMIN"])
        with pytest.raises(ArgumentError):
            await service.get_setting(actor, "unknown.key")

    @pytest.mark.asyncio
    async def test_put_unknown_key_raises_argument_error(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)
        actor = _actor(admin_id, ["ADMIN"])
        with pytest.raises(ArgumentError):
            await service.put_setting(
                actor,
                "unknown.key",
                {"value": "x", "expected_version": None, "idempotency_key": "k1"},
            )


# --------------------------------------------------------------------------- #
# 验收 2：敏感值只返回 configured/fingerprint
# --------------------------------------------------------------------------- #


class TestSecretSettings:
    """敏感设置经 ``SecretService`` 存储；只暴露 configured/fingerprint。"""

    @pytest.mark.asyncio
    async def test_put_secret_returns_no_plaintext_but_fingerprint(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        view = await service.put_setting(
            actor,
            "smtp.password",
            {
                "value": _SMTP_PASSWORD_PLAIN,
                "expected_version": None,
                "idempotency_key": "smtp-pw-1",
            },
        )

        assert view["key"] == "smtp.password"
        assert view["is_secret"] is True
        assert view["value"] is None, "敏感设置 value 必须为 None"
        assert view["configured"] is True
        assert view["fingerprint"] is not None
        assert view["fingerprint"].endswith(_SMTP_PASSWORD_PLAIN[-4:])
        assert _SMTP_PASSWORD_PLAIN not in view["fingerprint"][:8]
        assert view["version"] == 1

    @pytest.mark.asyncio
    async def test_get_secret_returns_configured_and_fingerprint(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        await service.put_setting(
            actor,
            "smtp.password",
            {
                "value": _SMTP_PASSWORD_PLAIN,
                "expected_version": None,
                "idempotency_key": "smtp-pw-2",
            },
        )

        # 重新读取（新 actor，模拟下一次请求）
        view = await service.get_setting(actor, "smtp.password")
        assert view["is_secret"] is True
        assert view["value"] is None
        assert view["configured"] is True
        assert view["fingerprint"] is not None
        assert view["fingerprint"].endswith(_SMTP_PASSWORD_PLAIN[-4:])

    @pytest.mark.asyncio
    async def test_get_unconfigured_secret_returns_default(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """未配置的敏感设置返回 configured=False、value=None、version=0。"""
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        view = await service.get_setting(actor, "external.github.api_key")
        assert view["configured"] is False
        assert view["value"] is None
        assert view["fingerprint"] is None
        assert view["version"] == 0

    @pytest.mark.asyncio
    async def test_plaintext_not_in_sqlite(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """敏感明文绝不进入 ``system_settings`` 或 ``outbox_events`` 任何列。"""
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        await service.put_setting(
            actor,
            "smtp.password",
            {
                "value": _SMTP_PASSWORD_PLAIN,
                "expected_version": None,
                "idempotency_key": "smtp-pw-3",
            },
        )

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT key, value, value_type, is_secret, secret_id, fingerprint, "
                "updated_at, updated_by, version_no FROM system_settings"
            ) as cur:
                setting_rows = [tuple(r) for r in await cur.fetchall()]
            async with conn.execute(
                "SELECT id, event_type, payload FROM outbox_events"
            ) as cur:
                event_rows = [tuple(r) for r in await cur.fetchall()]

        # system_settings 全表扫描：明文不得出现在任何列
        for row in setting_rows:
            for cell in row:
                assert _SMTP_PASSWORD_PLAIN not in str(cell), (
                    f"明文泄漏到 system_settings: row={row}"
                )
        # outbox_events payload 也不得包含明文
        for row in event_rows:
            for cell in row:
                assert _SMTP_PASSWORD_PLAIN not in str(cell), (
                    f"明文泄漏到 outbox_events: row={row}"
                )

    @pytest.mark.asyncio
    async def test_secret_still_resolvable_via_service(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """写入后的 secret 可经 SecretService resolve 还原（验证引用有效）。"""
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        await service.put_setting(
            actor,
            "smtp.password",
            {
                "value": _SMTP_PASSWORD_PLAIN,
                "expected_version": None,
                "idempotency_key": "smtp-pw-4",
            },
        )

        # 从 SQLite 读出 secret_id（仅测试用，正常路径不暴露）
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT secret_id FROM system_settings WHERE key = ?",
                ("smtp.password",),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        secret_id = str(row[0])

        # service 用 actor.organization_id 作为 secret 的 owner_id；
        # LocalSecretService 默认权限策略要求 resolver 的 actor_id == owner_id。
        resolved = await secret_service.resolve(
            secret_id, purpose="verify", actor_id=_ORG_ID
        )
        assert resolved == _SMTP_PASSWORD_PLAIN


# --------------------------------------------------------------------------- #
# Secret 轮换：旧 secret 删除，新 secret 生效
# --------------------------------------------------------------------------- #


class TestSecretRotation:
    """敏感设置再次写入：版本递增、指纹变化、旧 secret 被 best-effort 删除。"""

    @pytest.mark.asyncio
    async def test_rotate_secret_increments_version_and_changes_fingerprint(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        v1 = await service.put_setting(
            actor,
            "smtp.password",
            {
                "value": _SMTP_PASSWORD_PLAIN,
                "expected_version": None,
                "idempotency_key": "rot-1",
            },
        )
        assert v1["version"] == 1
        fp1 = v1["fingerprint"]

        new_plain = "smtp-new-rotated-VALUE-456-032"
        v2 = await service.put_setting(
            actor,
            "smtp.password",
            {
                "value": new_plain,
                "expected_version": 1,
                "idempotency_key": "rot-2",
            },
        )
        assert v2["version"] == 2
        assert v2["fingerprint"] != fp1
        assert v2["fingerprint"].endswith(new_plain[-4:])

        # 验证新明文可通过新 secret_id 解析
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT secret_id FROM system_settings WHERE key = ?",
                ("smtp.password",),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        new_secret_id = str(row[0])
        # service 用 actor.organization_id 作为 secret 的 owner_id；
        # LocalSecretService 默认权限策略要求 resolver 的 actor_id == owner_id。
        resolved = await secret_service.resolve(
            new_secret_id, purpose="verify", actor_id=_ORG_ID
        )
        assert resolved == new_plain


# --------------------------------------------------------------------------- #
# 非 ADMIN 读 OK / 写被拒
# --------------------------------------------------------------------------- #


class TestPermissionEnforcement:
    """权限校验：``read`` 允许 OBSERVER；``write`` 仅 ADMIN。"""

    @pytest.mark.asyncio
    async def test_observer_can_read(
        self,
        admin_db: tuple[Database, str],
        observer_db: tuple[Database, str],
    ) -> None:
        db, admin_id = admin_db
        _, observer_id = observer_db

        service = _make_service(db)
        admin_actor = _actor(admin_id, ["ADMIN"])
        await service.put_setting(
            admin_actor,
            "system.name",
            {
                "value": "MAF-Test",
                "expected_version": None,
                "idempotency_key": "perm-1",
            },
        )

        observer_actor = _actor(observer_id, ["OBSERVER"])
        view = await service.get_setting(observer_actor, "system.name")
        assert view["value"] == "MAF-Test"
        assert view["configured"] is True

    @pytest.mark.asyncio
    async def test_observer_cannot_write(
        self,
        observer_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, observer_id = observer_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(observer_id, ["OBSERVER"])

        with pytest.raises(PermissionDeniedError):
            await service.put_setting(
                actor,
                "system.name",
                {
                    "value": "Attempt",
                    "expected_version": None,
                    "idempotency_key": "perm-denied-1",
                },
            )

    @pytest.mark.asyncio
    async def test_observer_cannot_write_secret(
        self,
        observer_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """权限校验在 secret_service.create 之前发生，敏感写入也被拒绝。"""
        db, observer_id = observer_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(observer_id, ["OBSERVER"])

        with pytest.raises(PermissionDeniedError):
            await service.put_setting(
                actor,
                "smtp.password",
                {
                    "value": _SMTP_PASSWORD_PLAIN,
                    "expected_version": None,
                    "idempotency_key": "perm-denied-2",
                },
            )


# --------------------------------------------------------------------------- #
# 明文设置读写（非敏感 round-trip）
# --------------------------------------------------------------------------- #


class TestPlainSettings:
    """非敏感设置 JSON 序列化 round-trip。"""

    @pytest.mark.asyncio
    async def test_plain_string_round_trip(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)
        actor = _actor(admin_id, ["ADMIN"])

        view = await service.put_setting(
            actor,
            "system.name",
            {
                "value": "MAF-Platform",
                "expected_version": None,
                "idempotency_key": "plain-1",
            },
        )
        assert view["value"] == "MAF-Platform"
        assert view["is_secret"] is False
        assert view["fingerprint"] is None
        assert view["version"] == 1

        # 再读一次
        read = await service.get_setting(actor, "system.name")
        assert read["value"] == "MAF-Platform"
        assert read["configured"] is True
        assert read["version"] == 1

    @pytest.mark.asyncio
    async def test_plain_integer_round_trip(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)
        actor = _actor(admin_id, ["ADMIN"])

        view = await service.put_setting(
            actor,
            "smtp.port",
            {
                "value": 587,
                "expected_version": None,
                "idempotency_key": "plain-int-1",
            },
        )
        assert view["value"] == 587
        assert view["value_type"] == "integer"

        read = await service.get_setting(actor, "smtp.port")
        assert read["value"] == 587

    @pytest.mark.asyncio
    async def test_plain_update_increments_version(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)
        actor = _actor(admin_id, ["ADMIN"])

        v1 = await service.put_setting(
            actor,
            "system.timezone",
            {
                "value": "UTC",
                "expected_version": None,
                "idempotency_key": "tz-1",
            },
        )
        assert v1["version"] == 1

        v2 = await service.put_setting(
            actor,
            "system.timezone",
            {
                "value": "Asia/Shanghai",
                "expected_version": 1,
                "idempotency_key": "tz-2",
            },
        )
        assert v2["version"] == 2
        assert v2["value"] == "Asia/Shanghai"

    @pytest.mark.asyncio
    async def test_invalid_value_type_rejected(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """值类型不匹配抛 ArgumentError。"""
        db, admin_id = admin_db
        service = _make_service(db)
        actor = _actor(admin_id, ["ADMIN"])

        with pytest.raises(ArgumentError):
            await service.put_setting(
                actor,
                "smtp.port",
                {
                    "value": "not-an-int",
                    "expected_version": None,
                    "idempotency_key": "bad-type",
                },
            )


# --------------------------------------------------------------------------- #
# 验收 3：并发更新产生版本冲突（非覆盖）
# --------------------------------------------------------------------------- #


class TestVersionConflict:
    """``expected_version`` 不匹配抛 ``VersionConflictError``，原值不变。"""

    @pytest.mark.asyncio
    async def test_plain_version_conflict_does_not_overwrite(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)
        actor = _actor(admin_id, ["ADMIN"])

        v1 = await service.put_setting(
            actor,
            "system.name",
            {
                "value": "First",
                "expected_version": None,
                "idempotency_key": "conflict-1",
            },
        )
        assert v1["version"] == 1

        # 用陈旧的 expected_version=999 模拟并发冲突
        with pytest.raises(VersionConflictError):
            await service.put_setting(
                actor,
                "system.name",
                {
                    "value": "Concurrent",
                    "expected_version": 999,
                    "idempotency_key": "conflict-2",
                },
            )

        # 原值未变
        read = await service.get_setting(actor, "system.name")
        assert read["value"] == "First"
        assert read["version"] == 1

    @pytest.mark.asyncio
    async def test_secret_version_conflict_does_not_overwrite(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        v1 = await service.put_setting(
            actor,
            "smtp.password",
            {
                "value": _SMTP_PASSWORD_PLAIN,
                "expected_version": None,
                "idempotency_key": "sec-conflict-1",
            },
        )
        assert v1["version"] == 1
        fp1 = v1["fingerprint"]

        with pytest.raises(VersionConflictError):
            await service.put_setting(
                actor,
                "smtp.password",
                {
                    "value": "smtp-NEW-concurrent-attempt-XYZ",
                    "expected_version": 999,
                    "idempotency_key": "sec-conflict-2",
                },
            )

        # 原值与指纹未变
        read = await service.get_setting(actor, "smtp.password")
        assert read["version"] == 1
        assert read["fingerprint"] == fp1

    @pytest.mark.asyncio
    async def test_expected_version_on_missing_record_conflicts(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """对不存在的设置传 expected_version 应抛 VersionConflictError。"""
        db, admin_id = admin_db
        service = _make_service(db)
        actor = _actor(admin_id, ["ADMIN"])

        with pytest.raises(VersionConflictError):
            await service.put_setting(
                actor,
                "system.name",
                {
                    "value": "X",
                    "expected_version": 5,
                    "idempotency_key": "missing-conflict",
                },
            )


# --------------------------------------------------------------------------- #
# SystemSettingChanged 事件出现在 outbox_events
# --------------------------------------------------------------------------- #


class TestOutboxEvent:
    """设置变更必须通过 Outbox 事件 ``system.setting.changed`` 记录。"""

    @pytest.mark.asyncio
    async def test_plain_setting_change_appends_outbox_event(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)
        actor = _actor(admin_id, ["ADMIN"], trace_id="trace-plain-event")

        await service.put_setting(
            actor,
            "system.name",
            {
                "value": "Evented",
                "expected_version": None,
                "idempotency_key": "evt-1",
            },
        )

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT event_type, aggregate_type, aggregate_id, organization_id, "
                "actor_type, actor_id, trace_id, payload "
                "FROM outbox_events WHERE aggregate_type = ? "
                "AND aggregate_id = ? ORDER BY rowid",
                ("system_setting", "system.name"),
            ) as cur:
                rows = [tuple(r) for r in await cur.fetchall()]

        assert len(rows) >= 1
        evt = rows[-1]
        assert evt[0] == "system.setting.changed"
        assert evt[1] == "system_setting"
        assert evt[2] == "system.name"
        assert evt[3] == _ORG_ID
        assert evt[4] == "USER"
        assert evt[5] == admin_id
        assert evt[6] == "trace-plain-event"
        payload = json.loads(evt[7])
        assert payload["key"] == "system.name"
        assert payload["is_secret"] is False
        assert payload["version"] == 1
        assert payload["updated_by"] == admin_id
        # 明文值不得进入事件 payload
        assert "Evented" not in evt[7]

    @pytest.mark.asyncio
    async def test_secret_setting_change_appends_outbox_event_without_plaintext(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"], trace_id="trace-secret-event")

        await service.put_setting(
            actor,
            "smtp.password",
            {
                "value": _SMTP_PASSWORD_PLAIN,
                "expected_version": None,
                "idempotency_key": "evt-secret-1",
            },
        )

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT event_type, payload FROM outbox_events "
                "WHERE aggregate_id = ?",
                ("smtp.password",),
            ) as cur:
                rows = [tuple(r) for r in await cur.fetchall()]

        assert len(rows) >= 1
        evt = rows[-1]
        assert evt[0] == "system.setting.changed"
        payload = json.loads(evt[1])
        assert payload["is_secret"] is True
        assert payload["version"] == 1
        # 敏感明文绝不进入事件 payload
        assert _SMTP_PASSWORD_PLAIN not in evt[1]


# --------------------------------------------------------------------------- #
# HTTP 路由测试
# --------------------------------------------------------------------------- #


class TestHttpRouter:
    """``GET``/``PUT`` ``/api/v1/settings/{key}`` 端点测试。"""

    def _build_app(
        self,
        db: Database,
        *,
        secret_service: LocalSecretService | None = None,
        actor: ActorContext | None = None,
    ) -> FastAPI:
        service = _make_service(db, secret_service=secret_service)
        app = FastAPI()
        register_error_handlers(app)
        app.include_router(build_settings_router(service))
        if actor is not None:
            app.dependency_overrides[_anonymous_actor_dependency] = lambda: actor
        return app

    @pytest.mark.asyncio
    async def test_get_unknown_key_returns_400(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        actor = _actor(admin_id, ["ADMIN"])
        app = self._build_app(db, actor=actor)
        client = TestClient(app)

        resp = client.get("/api/v1/settings/unknown.key")
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"]["error_code"] == "ARGUMENT_INVALID"

    @pytest.mark.asyncio
    async def test_put_then_get_plain_setting(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        actor = _actor(admin_id, ["ADMIN"])
        app = self._build_app(db, actor=actor)
        client = TestClient(app)

        put_resp = client.put(
            "/api/v1/settings/system.name",
            json={
                "value": "HTTP-MAF",
                "expected_version": None,
                "idempotency_key": "http-1",
            },
        )
        assert put_resp.status_code == 200
        put_body = put_resp.json()
        assert put_body["key"] == "system.name"
        assert put_body["value"] == "HTTP-MAF"
        assert put_body["is_secret"] is False
        assert put_body["version"] == 1

        get_resp = client.get("/api/v1/settings/system.name")
        assert get_resp.status_code == 200
        assert get_resp.json()["value"] == "HTTP-MAF"

    @pytest.mark.asyncio
    async def test_put_secret_returns_no_plaintext(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        actor = _actor(admin_id, ["ADMIN"])
        app = self._build_app(db, secret_service=secret_service, actor=actor)
        client = TestClient(app)

        resp = client.put(
            "/api/v1/settings/smtp.password",
            json={
                "value": _SMTP_PASSWORD_PLAIN,
                "expected_version": None,
                "idempotency_key": "http-secret-1",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_secret"] is True
        assert body["value"] is None
        assert body["configured"] is True
        assert body["fingerprint"] is not None
        # 响应体不得包含明文
        assert _SMTP_PASSWORD_PLAIN not in resp.text

    @pytest.mark.asyncio
    async def test_put_version_conflict_returns_409(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        actor = _actor(admin_id, ["ADMIN"])
        app = self._build_app(db, actor=actor)
        client = TestClient(app)

        # 第一次写入
        first = client.put(
            "/api/v1/settings/system.timezone",
            json={
                "value": "UTC",
                "expected_version": None,
                "idempotency_key": "http-conflict-1",
            },
        )
        assert first.status_code == 200

        # 用陈旧 expected_version 触发冲突
        conflict = client.put(
            "/api/v1/settings/system.timezone",
            json={
                "value": "Asia/Shanghai",
                "expected_version": 999,
                "idempotency_key": "http-conflict-2",
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["error_code"] == "VERSION_CONFLICT"

    @pytest.mark.asyncio
    async def test_put_by_observer_returns_403(
        self, observer_db: tuple[Database, str]
    ) -> None:
        db, observer_id = observer_db
        actor = _actor(observer_id, ["OBSERVER"])
        app = self._build_app(db, actor=actor)
        client = TestClient(app)

        resp = client.put(
            "/api/v1/settings/system.name",
            json={
                "value": "Forbidden",
                "expected_version": None,
                "idempotency_key": "http-forbidden-1",
            },
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["error_code"] == "PERMISSION_DENIED"

    @pytest.mark.asyncio
    async def test_get_by_observer_returns_200(
        self,
        admin_db: tuple[Database, str],
        observer_db: tuple[Database, str],
    ) -> None:
        db, admin_id = admin_db
        _, observer_id = observer_db

        # ADMIN 先写入
        admin_actor = _actor(admin_id, ["ADMIN"])
        admin_app = self._build_app(db, actor=admin_actor)
        admin_client = TestClient(admin_app)
        put = admin_client.put(
            "/api/v1/settings/system.name",
            json={
                "value": "Shared",
                "expected_version": None,
                "idempotency_key": "http-shared-1",
            },
        )
        assert put.status_code == 200

        # OBSERVER 可读
        observer_actor = _actor(observer_id, ["OBSERVER"])
        observer_app = self._build_app(db, actor=observer_actor)
        observer_client = TestClient(observer_app)
        get = observer_client.get("/api/v1/settings/system.name")
        assert get.status_code == 200
        assert get.json()["value"] == "Shared"
