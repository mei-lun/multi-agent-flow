"""TASK-037 集成测试：模型连接管理（ModelConnectionService）。

验收标准覆盖：

1. 创建连接：响应不含 ``credential_value``；初始状态 ``UNVERIFIED``；
   ``credential_fingerprint`` 非空且为不可逆指纹（``sha256(plaintext)[:8] + ".." + plaintext[-4:]``）。
2. URL/TLS 与权限策略生效：``provider``/``credential_type`` 取值非法抛 ``ArgumentError``；
   非 ADMIN/DESIGNER 写入抛 ``PermissionDeniedError``。
3. 凭据安全：明文绝不进入 ``model_connections`` 表任何列、``outbox_events.payload`` 或
   ``ModelConnectionView`` 任何字段；数据库只保存 ``credential_secret_id`` 引用与
   不可逆 ``credential_fingerprint``。
4. 乐观锁：``update_connection``/``delete_connection`` 的 ``expected_version`` 不匹配抛
   ``VersionConflictError``，原值不变。
5. 事件：``model_connection.created``/``updated``/``deleted`` 三类事件出现在
   ``outbox_events``，payload 不含明文。
6. ``test_connection``：凭据可解析且 URL 合法 → ``VERIFIED``；否则 ``ERROR``。
   状态更新不递增 ``version_no``。
7. Secret 轮换：更新凭据产生新 ``secret_id`` 与新 ``fingerprint``；
   旧 secret 被 best-effort 删除（不再可 ``resolve``）。

补充覆盖：
- HTTP 路由：``POST``/``GET``/``PATCH``/``DELETE`` ``/api/v1/model-connections`` 与
  ``POST /api/v1/model-connections/{id}/test``；
- 凭据明文绝不进入 HTTP 响应正文；
- DESIGNER 可读写；OBSERVER 只读；无角色被拒绝。

测试范围：
- ``apps/server/src/maf_server/modules/model_connections/{schemas,repository,service,router}.py``；
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
    NotFoundError,
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
from maf_server.modules.iam.repository import init_schema as init_iam_schema
from maf_server.modules.iam.service import seed_local_user
from maf_server.modules.model_connections.repository import init_schema as init_mc_schema
from maf_server.modules.model_connections.router import (
    _anonymous_actor_dependency,
    build_model_connection_router,
)
from maf_server.modules.model_connections.service import (
    ModelConnectionServiceImpl,
    ensure_schema,
)

_SECRET_PLAINTEXT = "test-secret-for-model-connections-task-037"
_TEST_PASSWORD = "mc-correct-horse-battery-staple"
_ORG_ID = "org-001"

#: 凭据明文样本（足够长，使指纹末 4 位唯一且不构成前缀泄露）。
_CRED_OPENAI = "sk-OPENAI-task037-FAKE-SECRET-1234567890"
_CRED_OPENAI_ROTATED = "sk-OPENAI-ROTATED-task037-FAKE-SECRET-0987654321"
_CRED_ANTHROPIC = "sk-ANTHROPIC-task037-FAKE-SECRET-abcdefghij"


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
    """已初始化并建好 IAM + model_connections + outbox 表的 Database。"""
    settings = _make_settings(tmp_path)
    database = Database(settings)
    await database.initialize()
    async with database.write_connection() as conn:
        await init_iam_schema(conn)
        await init_mc_schema(conn)
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
async def designer_db(db: Database) -> tuple[Database, str]:
    """种子一个 DESIGNER 用户；返回 (db, designer_user_id)。"""
    designer_id = await seed_local_user(
        db,
        username="designer",
        display_name="Designer User",
        password_plain=_TEST_PASSWORD,
        permission_keys=["DESIGNER"],
    )
    return db, designer_id


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
    """基于 AES-GCM 文件后端的 ``LocalSecretService``，不依赖真实 keyring。"""
    master_key = _secrets.token_bytes(MASTER_KEY_SIZE_BYTES)
    store = AesGcmFileStore(
        master_key=master_key,
        storage_dir=tmp_path / "secrets",
        organization_id=_ORG_ID,
    )
    return LocalSecretService(primary=store)


def _actor(
    user_id: str, roles: list[str], trace_id: str = "mc-trace"
) -> ActorContext:
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
) -> ModelConnectionServiceImpl:
    """构造 ``ModelConnectionServiceImpl``，可选注入 ``secret_service``。"""
    return ModelConnectionServiceImpl(db, secret_service=secret_service)


async def _create_one(
    service: ModelConnectionServiceImpl,
    actor: ActorContext,
    *,
    name: str = "openai-default",
    provider: str = "openai",
    model_id: str = "gpt-4o",
    api_base: str = "https://api.openai.com/v1",
    credential_type: str = "api_key",
    credential_value: str = _CRED_OPENAI,
) -> str:
    """辅助：创建一条连接并返回 connection_id。"""
    view = await service.create_connection(
        name=name,
        provider=provider,
        model_id=model_id,
        api_base=api_base,
        credential_type=credential_type,
        credential_value=credential_value,
        actor=actor,
    )
    return view["id"]


# --------------------------------------------------------------------------- #
# 验收 1：创建连接 —— 响应不含明文，初始状态 UNVERIFIED
# --------------------------------------------------------------------------- #


class TestCreateConnection:
    """``create_connection`` 基本行为与安全约束。"""

    @pytest.mark.asyncio
    async def test_create_returns_view_without_credential_value(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        view = await service.create_connection(
            name="openai-default",
            provider="openai",
            model_id="gpt-4o",
            api_base="https://api.openai.com/v1",
            credential_type="api_key",
            credential_value=_CRED_OPENAI,
            actor=actor,
        )

        assert view["name"] == "openai-default"
        assert view["provider"] == "openai"
        assert view["model_id"] == "gpt-4o"
        assert view["api_base"] == "https://api.openai.com/v1"
        assert view["credential_type"] == "api_key"
        assert view["status"] == "UNVERIFIED"
        assert view["version"] == 1
        assert view["created_by"] == admin_id
        # 安全：响应不含明文与 secret_id
        assert "credential_value" not in view
        assert "secret_id" not in view
        assert "credential_secret_id" not in view
        # 指纹非空且为不可逆指纹
        assert view["credential_fingerprint"] is not None
        fp = view["credential_fingerprint"]
        assert fp.endswith(_CRED_OPENAI[-4:])
        assert _CRED_OPENAI[:8] not in fp  # 明文前缀不在指纹中
        assert _CRED_OPENAI not in fp

    @pytest.mark.asyncio
    async def test_create_generates_uuid_id(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        id1 = await _create_one(service, actor, name="c1")
        id2 = await _create_one(service, actor, name="c2")
        assert id1 != id2
        # UUID v4 字符串格式：36 字符，含 4 个连字符
        assert len(id1) == 36 and id1.count("-") == 4

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "provider,credential_type",
        [
            ("unknown_provider", "api_key"),
            ("openai", "unknown_type"),
            ("", "api_key"),
            ("openai", ""),
        ],
    )
    async def test_create_invalid_provider_or_credential_type_rejected(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
        provider: str,
        credential_type: str,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        with pytest.raises(ArgumentError):
            await service.create_connection(
                name="bad",
                provider=provider,
                model_id="gpt-4o",
                api_base="https://api.openai.com/v1",
                credential_type=credential_type,
                credential_value=_CRED_OPENAI,
                actor=actor,
            )

    @pytest.mark.asyncio
    async def test_create_invalid_url_rejected_at_test(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """``api_base`` 非法 URL 在创建时不抛（service 不强制 URL 校验），
        但 ``test_connection`` 会标记为 ERROR。这里验证创建接受任意非空字符串。"""
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        view = await service.create_connection(
            name="bad-url",
            provider="local",
            model_id="llama3",
            api_base="not-a-url",
            credential_type="bearer_token",
            credential_value=_CRED_OPENAI,
            actor=actor,
        )
        assert view["status"] == "UNVERIFIED"


# --------------------------------------------------------------------------- #
# 验收 2：权限策略生效
# --------------------------------------------------------------------------- #


class TestPermissionEnforcement:
    """RBAC 权限校验。"""

    @pytest.mark.asyncio
    async def test_admin_can_create(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])
        cid = await _create_one(service, actor)
        assert cid

    @pytest.mark.asyncio
    async def test_designer_can_create(
        self,
        designer_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, designer_id = designer_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(designer_id, ["DESIGNER"])
        cid = await _create_one(service, actor)
        assert cid

    @pytest.mark.asyncio
    async def test_observer_cannot_create(
        self,
        observer_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, observer_id = observer_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(observer_id, ["OBSERVER"])

        with pytest.raises(PermissionDeniedError):
            await service.create_connection(
                name="obs-attempt",
                provider="openai",
                model_id="gpt-4o",
                api_base="https://api.openai.com/v1",
                credential_type="api_key",
                credential_value=_CRED_OPENAI,
                actor=actor,
            )

    @pytest.mark.asyncio
    async def test_observer_can_read(
        self,
        admin_db: tuple[Database, str],
        observer_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        _, observer_id = observer_db
        service = _make_service(db, secret_service=secret_service)

        admin_actor = _actor(admin_id, ["ADMIN"])
        cid = await _create_one(service, admin_actor)

        observer_actor = _actor(observer_id, ["OBSERVER"])
        view = await service.get_connection(cid, actor=observer_actor)
        assert view["id"] == cid

        views = await service.list_connections(actor=observer_actor)
        assert any(v["id"] == cid for v in views)

    @pytest.mark.asyncio
    async def test_observer_cannot_update_or_delete(
        self,
        admin_db: tuple[Database, str],
        observer_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        _, observer_id = observer_db
        service = _make_service(db, secret_service=secret_service)

        admin_actor = _actor(admin_id, ["ADMIN"])
        cid = await _create_one(service, admin_actor)

        observer_actor = _actor(observer_id, ["OBSERVER"])
        with pytest.raises(PermissionDeniedError):
            await service.update_connection(
                cid, name="hacked", expected_version=1, actor=observer_actor
            )
        with pytest.raises(PermissionDeniedError):
            await service.delete_connection(cid, 1, actor=observer_actor)

    @pytest.mark.asyncio
    async def test_observer_can_test_connection(
        self,
        admin_db: tuple[Database, str],
        observer_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """``test_connection`` 是 read 操作；OBSERVER 可调用。"""
        db, admin_id = admin_db
        _, observer_id = observer_db
        service = _make_service(db, secret_service=secret_service)

        admin_actor = _actor(admin_id, ["ADMIN"])
        cid = await _create_one(service, admin_actor)

        observer_actor = _actor(observer_id, ["OBSERVER"])
        result = await service.test_connection(cid, actor=observer_actor)
        assert result["connection_id"] == cid

    @pytest.mark.asyncio
    async def test_unauthenticated_denied(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, _ = admin_db
        service = _make_service(db, secret_service=secret_service)
        # 缺失 user_id
        bad_actor = ActorContext(
            user_id="",
            organization_id=_ORG_ID,
            permission_keys=["ADMIN"],
            trace_id="t",
        )
        with pytest.raises(PermissionDeniedError):
            await service.create_connection(
                name="bad",
                provider="openai",
                model_id="gpt-4o",
                api_base="https://api.openai.com/v1",
                credential_type="api_key",
                credential_value=_CRED_OPENAI,
                actor=bad_actor,
            )


# --------------------------------------------------------------------------- #
# 验收 3：凭据安全 —— 明文绝不进入 SQLite/事件/响应
# --------------------------------------------------------------------------- #


class TestCredentialSecurity:
    """凭据明文绝不持久化、绝不进入事件 payload、绝不进入响应。"""

    @pytest.mark.asyncio
    async def test_plaintext_not_in_sqlite(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """明文不得出现在 ``model_connections`` 或 ``outbox_events`` 任何列。"""
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        await _create_one(service, actor, credential_value=_CRED_OPENAI)

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT id, name, provider, model_id, api_base, credential_type, "
                "credential_secret_id, credential_fingerprint, status, created_by, "
                "created_at, updated_at, version_no FROM model_connections"
            ) as cur:
                mc_rows = [tuple(r) for r in await cur.fetchall()]
            async with conn.execute(
                "SELECT id, event_type, payload FROM outbox_events"
            ) as cur:
                event_rows = [tuple(r) for r in await cur.fetchall()]

        for row in mc_rows:
            for cell in row:
                assert _CRED_OPENAI not in str(cell), (
                    f"明文泄漏到 model_connections: row={row}"
                )
        for row in event_rows:
            for cell in row:
                assert _CRED_OPENAI not in str(cell), (
                    f"明文泄漏到 outbox_events: row={row}"
                )

    @pytest.mark.asyncio
    async def test_secret_id_stored_in_db_not_plaintext(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """``credential_secret_id`` 是 opaque 引用，不是明文。"""
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor, credential_value=_CRED_OPENAI)

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT credential_secret_id, credential_fingerprint "
                "FROM model_connections WHERE id = ?",
                (cid,),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        secret_id = str(row[0])
        fingerprint = str(row[1])
        # secret_id 不等于明文
        assert secret_id != _CRED_OPENAI
        assert _CRED_OPENAI not in secret_id
        # 指纹不等于明文
        assert fingerprint != _CRED_OPENAI
        assert _CRED_OPENAI not in fingerprint

    @pytest.mark.asyncio
    async def test_secret_resolvable_via_service(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """写入后的 secret 可经 SecretService resolve 还原（验证引用有效）。"""
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor, credential_value=_CRED_OPENAI)

        # 从 SQLite 读出 secret_id（仅测试用，正常路径不暴露）
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT credential_secret_id FROM model_connections WHERE id = ?",
                (cid,),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        secret_id = str(row[0])

        # service 用 connection_id 作为 secret 的 owner_id；
        # LocalSecretService 默认权限策略要求 resolver 的 actor_id == owner_id。
        resolved = await secret_service.resolve(
            secret_id, purpose="verify", actor_id=cid
        )
        assert resolved == _CRED_OPENAI


# --------------------------------------------------------------------------- #
# CRUD：get / list / update / delete
# --------------------------------------------------------------------------- #


class TestCrudOperations:
    """``get``/``list``/``update``/``delete`` 基本行为。"""

    @pytest.mark.asyncio
    async def test_get_nonexistent_raises_not_found(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        with pytest.raises(NotFoundError):
            await service.get_connection("nonexistent-id", actor=actor)

    @pytest.mark.asyncio
    async def test_list_returns_all_ordered_by_created_at(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        id1 = await _create_one(service, actor, name="first")
        id2 = await _create_one(service, actor, name="second")
        id3 = await _create_one(service, actor, name="third")

        views = await service.list_connections(actor=actor)
        assert len(views) == 3
        # 按创建时间升序
        assert views[0]["id"] == id1
        assert views[1]["id"] == id2
        assert views[2]["id"] == id3

    @pytest.mark.asyncio
    async def test_update_name_increments_version(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor, name="original")
        view = await service.update_connection(
            cid, name="updated", expected_version=1, actor=actor
        )
        assert view["name"] == "updated"
        assert view["version"] == 2
        # 指纹未变（未轮换凭据）
        assert view["credential_fingerprint"] is not None

    @pytest.mark.asyncio
    async def test_update_api_base(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor)
        view = await service.update_connection(
            cid,
            api_base="https://api.openai.com/v2",
            expected_version=1,
            actor=actor,
        )
        assert view["api_base"] == "https://api.openai.com/v2"
        assert view["version"] == 2

    @pytest.mark.asyncio
    async def test_update_requires_at_least_one_field(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor)
        with pytest.raises(ArgumentError):
            await service.update_connection(
                cid, expected_version=1, actor=actor
            )

    @pytest.mark.asyncio
    async def test_delete_removes_connection(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor)
        await service.delete_connection(cid, 1, actor=actor)

        with pytest.raises(NotFoundError):
            await service.get_connection(cid, actor=actor)

        views = await service.list_connections(actor=actor)
        assert len(views) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent_raises_not_found(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        with pytest.raises(NotFoundError):
            await service.delete_connection("nonexistent-id", 1, actor=actor)


# --------------------------------------------------------------------------- #
# 验收 4：乐观锁 —— 版本不匹配抛 VersionConflictError
# --------------------------------------------------------------------------- #


class TestOptimisticLocking:
    """``expected_version`` 不匹配抛 ``VersionConflictError``，原值不变。"""

    @pytest.mark.asyncio
    async def test_update_version_conflict_does_not_overwrite(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor, name="original")
        with pytest.raises(VersionConflictError):
            await service.update_connection(
                cid, name="concurrent", expected_version=999, actor=actor
            )

        view = await service.get_connection(cid, actor=actor)
        assert view["name"] == "original"
        assert view["version"] == 1

    @pytest.mark.asyncio
    async def test_update_with_credential_version_conflict(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """凭据轮换路径下的版本冲突。"""
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor, credential_value=_CRED_OPENAI)
        fp1 = (await service.get_connection(cid, actor=actor))["credential_fingerprint"]

        with pytest.raises(VersionConflictError):
            await service.update_connection(
                cid,
                credential_value=_CRED_OPENAI_ROTATED,
                expected_version=999,
                actor=actor,
            )

        # 原值与指纹未变
        view = await service.get_connection(cid, actor=actor)
        assert view["version"] == 1
        assert view["credential_fingerprint"] == fp1

    @pytest.mark.asyncio
    async def test_delete_version_conflict(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor)
        with pytest.raises(VersionConflictError):
            await service.delete_connection(cid, 999, actor=actor)

        # 原行未删除
        view = await service.get_connection(cid, actor=actor)
        assert view["version"] == 1

    @pytest.mark.asyncio
    async def test_concurrent_updates_produce_conflict(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """模拟并发：第一个 update 成功递增版本，第二个用陈旧版本冲突。"""
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor, name="v1")
        # 第一次更新：version 1 → 2
        await service.update_connection(
            cid, name="v2", expected_version=1, actor=actor
        )
        # 第二次更新仍用 expected_version=1 → 冲突
        with pytest.raises(VersionConflictError):
            await service.update_connection(
                cid, name="v3-stale", expected_version=1, actor=actor
            )

        view = await service.get_connection(cid, actor=actor)
        assert view["name"] == "v2"
        assert view["version"] == 2


# --------------------------------------------------------------------------- #
# 验收 5：Outbox 事件
# --------------------------------------------------------------------------- #


class TestOutboxEvents:
    """``model_connection.created/updated/deleted`` 事件出现在 ``outbox_events``。"""

    @pytest.mark.asyncio
    async def test_created_event_appended(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"], trace_id="trace-create")

        cid = await _create_one(service, actor)

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT event_type, aggregate_type, aggregate_id, organization_id, "
                "actor_type, actor_id, trace_id, payload "
                "FROM outbox_events WHERE aggregate_type = ? AND aggregate_id = ?",
                ("model_connection", cid),
            ) as cur:
                rows = [tuple(r) for r in await cur.fetchall()]

        assert len(rows) >= 1
        evt = rows[-1]
        assert evt[0] == "model_connection.created"
        assert evt[1] == "model_connection"
        assert evt[2] == cid
        assert evt[3] == _ORG_ID
        assert evt[4] == "USER"
        assert evt[5] == admin_id
        assert evt[6] == "trace-create"
        payload = json.loads(evt[7])
        assert payload["connection_id"] == cid
        assert payload["version"] == 1
        # 明文不得进入事件 payload
        assert _CRED_OPENAI not in evt[7]

    @pytest.mark.asyncio
    async def test_updated_event_appended(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"], trace_id="trace-update")

        cid = await _create_one(service, actor, name="orig")
        await service.update_connection(
            cid, name="new", expected_version=1, actor=actor
        )

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT event_type, payload FROM outbox_events "
                "WHERE aggregate_id = ? AND event_type = ?",
                (cid, "model_connection.updated"),
            ) as cur:
                rows = [tuple(r) for r in await cur.fetchall()]

        assert len(rows) >= 1
        evt = rows[-1]
        payload = json.loads(evt[1])
        assert payload["connection_id"] == cid
        assert "name" in payload["changed"]
        assert payload["credential_rotated"] is False
        assert payload["version"] == 2

    @pytest.mark.asyncio
    async def test_updated_with_credential_event_marks_rotated(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor, credential_value=_CRED_OPENAI)
        await service.update_connection(
            cid,
            credential_value=_CRED_OPENAI_ROTATED,
            expected_version=1,
            actor=actor,
        )

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT event_type, payload FROM outbox_events "
                "WHERE aggregate_id = ? AND event_type = ?",
                (cid, "model_connection.updated"),
            ) as cur:
                rows = [tuple(r) for r in await cur.fetchall()]

        assert len(rows) >= 1
        evt = rows[-1]
        payload = json.loads(evt[1])
        assert payload["credential_rotated"] is True
        assert "credential" in payload["changed"]
        # 明文不得进入 payload
        assert _CRED_OPENAI not in evt[1]
        assert _CRED_OPENAI_ROTATED not in evt[1]

    @pytest.mark.asyncio
    async def test_deleted_event_appended(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"], trace_id="trace-delete")

        cid = await _create_one(service, actor)
        await service.delete_connection(cid, 1, actor=actor)

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT event_type, aggregate_id, actor_id, trace_id, payload "
                "FROM outbox_events WHERE aggregate_id = ? AND event_type = ?",
                (cid, "model_connection.deleted"),
            ) as cur:
                rows = [tuple(r) for r in await cur.fetchall()]

        assert len(rows) >= 1
        evt = rows[-1]
        assert evt[1] == cid
        assert evt[2] == admin_id
        assert evt[3] == "trace-delete"
        payload = json.loads(evt[4])
        assert payload["connection_id"] == cid


# --------------------------------------------------------------------------- #
# 验收 6：test_connection —— VERIFIED / ERROR
# --------------------------------------------------------------------------- #


class TestTestConnection:
    """``test_connection`` 验证凭据可解析与 URL 格式。"""

    @pytest.mark.asyncio
    async def test_valid_connection_verified(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(
            service,
            actor,
            api_base="https://api.openai.com/v1",
            credential_value=_CRED_OPENAI,
        )
        result = await service.test_connection(cid, actor=actor)
        assert result["ok"] is True
        assert result["status"] == "VERIFIED"
        assert result["connection_id"] == cid
        assert "checked_at" in result

        # 状态已更新到 DB
        view = await service.get_connection(cid, actor=actor)
        assert view["status"] == "VERIFIED"
        # test_connection 不递增 version_no
        assert view["version"] == 1

    @pytest.mark.asyncio
    async def test_invalid_url_marked_error(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(
            service,
            actor,
            provider="local",
            model_id="llama3",
            api_base="not-a-valid-url",
            credential_type="bearer_token",
            credential_value=_CRED_OPENAI,
        )
        result = await service.test_connection(cid, actor=actor)
        assert result["ok"] is False
        assert result["status"] == "ERROR"

        view = await service.get_connection(cid, actor=actor)
        assert view["status"] == "ERROR"
        # 状态更新不递增 version_no
        assert view["version"] == 1

    @pytest.mark.asyncio
    async def test_test_nonexistent_raises_not_found(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        with pytest.raises(NotFoundError):
            await service.test_connection("nonexistent", actor=actor)


# --------------------------------------------------------------------------- #
# 验收 7：凭据轮换 —— 旧 secret 删除，新 secret 生效
# --------------------------------------------------------------------------- #


class TestCredentialRotation:
    """更新凭据产生新 ``secret_id`` 与新 ``fingerprint``；旧 secret 被删除。"""

    @pytest.mark.asyncio
    async def test_rotate_changes_fingerprint_and_secret_id(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor, credential_value=_CRED_OPENAI)
        fp1 = (await service.get_connection(cid, actor=actor))["credential_fingerprint"]

        # 从 DB 读出旧 secret_id
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT credential_secret_id FROM model_connections WHERE id = ?",
                (cid,),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        old_secret_id = str(row[0])

        # 轮换凭据
        view = await service.update_connection(
            cid,
            credential_value=_CRED_OPENAI_ROTATED,
            expected_version=1,
            actor=actor,
        )
        assert view["version"] == 2
        assert view["credential_fingerprint"] != fp1
        assert view["credential_fingerprint"].endswith(_CRED_OPENAI_ROTATED[-4:])

        # 新 secret_id 与旧不同
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT credential_secret_id FROM model_connections WHERE id = ?",
                (cid,),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        new_secret_id = str(row[0])
        assert new_secret_id != old_secret_id

        # 新 secret 可解析出新明文
        resolved = await secret_service.resolve(
            new_secret_id, purpose="verify", actor_id=cid
        )
        assert resolved == _CRED_OPENAI_ROTATED

    @pytest.mark.asyncio
    async def test_rotate_deletes_old_secret(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """轮换后旧 secret_id 不再可 resolve（被 best-effort 删除）。"""
        db, admin_id = admin_db
        service = _make_service(db, secret_service=secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor, credential_value=_CRED_OPENAI)

        # 读出旧 secret_id
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT credential_secret_id FROM model_connections WHERE id = ?",
                (cid,),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        old_secret_id = str(row[0])

        # 轮换
        await service.update_connection(
            cid,
            credential_value=_CRED_OPENAI_ROTATED,
            expected_version=1,
            actor=actor,
        )

        # 旧 secret 已被删除，resolve 应抛 NotFoundError
        from maf_domain.errors import NotFoundError as _NotFoundError

        with pytest.raises(_NotFoundError):
            await secret_service.resolve(old_secret_id, purpose="verify", actor_id=cid)


# --------------------------------------------------------------------------- #
# HTTP 路由测试
# --------------------------------------------------------------------------- #


class TestHttpRouter:
    """``POST``/``GET``/``PATCH``/``DELETE``/``test`` HTTP 端点。"""

    def _build_app(
        self,
        db: Database,
        secret_service: LocalSecretService,
        *,
        actor: ActorContext,
    ) -> FastAPI:
        service = _make_service(db, secret_service=secret_service)
        app = FastAPI()
        register_error_handlers(app)
        app.include_router(build_model_connection_router(service))
        if actor is not None:
            app.dependency_overrides[_anonymous_actor_dependency] = lambda: actor
        return app

    @pytest.mark.asyncio
    async def test_post_returns_201_without_credential_value(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        actor = _actor(admin_id, ["ADMIN"])
        app = self._build_app(db, secret_service, actor=actor)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/model-connections",
            json={
                "name": "http-openai",
                "provider": "openai",
                "model_id": "gpt-4o",
                "api_base": "https://api.openai.com/v1",
                "credential_type": "api_key",
                "credential_value": _CRED_OPENAI,
                "idempotency_key": "http-create-1",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "http-openai"
        assert body["status"] == "UNVERIFIED"
        assert body["version"] == 1
        assert body["credential_fingerprint"] is not None
        # 响应正文不得包含明文
        assert "credential_value" not in body
        assert "secret_id" not in body
        assert _CRED_OPENAI not in resp.text

    @pytest.mark.asyncio
    async def test_post_invalid_provider_returns_400(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        actor = _actor(admin_id, ["ADMIN"])
        app = self._build_app(db, secret_service, actor=actor)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/model-connections",
            json={
                "name": "bad",
                "provider": "unknown",
                "model_id": "gpt-4o",
                "api_base": "https://api.openai.com/v1",
                "credential_type": "api_key",
                "credential_value": _CRED_OPENAI,
                "idempotency_key": "http-bad-1",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["error_code"] == "ARGUMENT_INVALID"

    @pytest.mark.asyncio
    async def test_post_by_observer_returns_403(
        self,
        observer_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, observer_id = observer_db
        actor = _actor(observer_id, ["OBSERVER"])
        app = self._build_app(db, secret_service, actor=actor)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/model-connections",
            json={
                "name": "forbidden",
                "provider": "openai",
                "model_id": "gpt-4o",
                "api_base": "https://api.openai.com/v1",
                "credential_type": "api_key",
                "credential_value": _CRED_OPENAI,
                "idempotency_key": "http-forbidden-1",
            },
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["error_code"] == "PERMISSION_DENIED"

    @pytest.mark.asyncio
    async def test_full_crud_via_http(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        actor = _actor(admin_id, ["ADMIN"])
        app = self._build_app(db, secret_service, actor=actor)
        client = TestClient(app)

        # POST 创建
        create = client.post(
            "/api/v1/model-connections",
            json={
                "name": "http-crud",
                "provider": "anthropic",
                "model_id": "claude-3-5-sonnet",
                "api_base": "https://api.anthropic.com",
                "credential_type": "api_key",
                "credential_value": _CRED_ANTHROPIC,
                "idempotency_key": "http-crud-1",
            },
        )
        assert create.status_code == 201
        cid = create.json()["id"]

        # GET 详情
        get_resp = client.get(f"/api/v1/model-connections/{cid}")
        assert get_resp.status_code == 200
        assert get_resp.json()["id"] == cid

        # GET 列表
        list_resp = client.get("/api/v1/model-connections")
        assert list_resp.status_code == 200
        assert any(v["id"] == cid for v in list_resp.json())

        # PATCH 更新
        patch_resp = client.patch(
            f"/api/v1/model-connections/{cid}",
            json={
                "name": "http-crud-updated",
                "expected_version": 1,
                "idempotency_key": "http-crud-2",
            },
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["name"] == "http-crud-updated"
        assert patch_resp.json()["version"] == 2

        # POST test
        test_resp = client.post(f"/api/v1/model-connections/{cid}/test")
        assert test_resp.status_code == 200
        assert test_resp.json()["ok"] is True
        assert test_resp.json()["status"] == "VERIFIED"

        # DELETE 删除（用查询参数 expected_version）
        del_resp = client.delete(
            f"/api/v1/model-connections/{cid}?expected_version=2"
        )
        assert del_resp.status_code == 204

        # 再次 GET 应 404
        get_after = client.get(f"/api/v1/model-connections/{cid}")
        assert get_after.status_code == 404
        assert get_after.json()["error"]["error_code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_patch_version_conflict_returns_409(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        actor = _actor(admin_id, ["ADMIN"])
        app = self._build_app(db, secret_service, actor=actor)
        client = TestClient(app)

        create = client.post(
            "/api/v1/model-connections",
            json={
                "name": "conflict-test",
                "provider": "openai",
                "model_id": "gpt-4o",
                "api_base": "https://api.openai.com/v1",
                "credential_type": "api_key",
                "credential_value": _CRED_OPENAI,
                "idempotency_key": "http-conflict-1",
            },
        )
        assert create.status_code == 201
        cid = create.json()["id"]

        conflict = client.patch(
            f"/api/v1/model-connections/{cid}",
            json={
                "name": "stale",
                "expected_version": 999,
                "idempotency_key": "http-conflict-2",
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["error_code"] == "VERSION_CONFLICT"

    @pytest.mark.asyncio
    async def test_patch_with_credential_returns_no_plaintext(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """PATCH 更新凭据：响应不含明文，指纹变化。"""
        db, admin_id = admin_db
        actor = _actor(admin_id, ["ADMIN"])
        app = self._build_app(db, secret_service, actor=actor)
        client = TestClient(app)

        create = client.post(
            "/api/v1/model-connections",
            json={
                "name": "rotate-test",
                "provider": "openai",
                "model_id": "gpt-4o",
                "api_base": "https://api.openai.com/v1",
                "credential_type": "api_key",
                "credential_value": _CRED_OPENAI,
                "idempotency_key": "http-rotate-1",
            },
        )
        assert create.status_code == 201
        cid = create.json()["id"]
        fp1 = create.json()["credential_fingerprint"]

        patch = client.patch(
            f"/api/v1/model-connections/{cid}",
            json={
                "credential_value": _CRED_OPENAI_ROTATED,
                "expected_version": 1,
                "idempotency_key": "http-rotate-2",
            },
        )
        assert patch.status_code == 200
        body = patch.json()
        assert body["version"] == 2
        assert body["credential_fingerprint"] != fp1
        # 响应正文不得包含任何明文
        assert _CRED_OPENAI not in patch.text
        assert _CRED_OPENAI_ROTATED not in patch.text

    @pytest.mark.asyncio
    async def test_get_by_observer_returns_200(
        self,
        admin_db: tuple[Database, str],
        observer_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """OBSERVER 可读连接，但不能创建。"""
        db, admin_id = admin_db
        _, observer_id = observer_db

        # ADMIN 创建
        admin_actor = _actor(admin_id, ["ADMIN"])
        admin_app = self._build_app(db, secret_service, actor=admin_actor)
        admin_client = TestClient(admin_app)
        create = admin_client.post(
            "/api/v1/model-connections",
            json={
                "name": "shared",
                "provider": "openai",
                "model_id": "gpt-4o",
                "api_base": "https://api.openai.com/v1",
                "credential_type": "api_key",
                "credential_value": _CRED_OPENAI,
                "idempotency_key": "http-shared-1",
            },
        )
        assert create.status_code == 201
        cid = create.json()["id"]

        # OBSERVER 可读
        observer_actor = _actor(observer_id, ["OBSERVER"])
        observer_app = self._build_app(db, secret_service, actor=observer_actor)
        observer_client = TestClient(observer_app)
        get = observer_client.get(f"/api/v1/model-connections/{cid}")
        assert get.status_code == 200
        assert get.json()["id"] == cid
        # 响应不含明文
        assert _CRED_OPENAI not in get.text

    @pytest.mark.asyncio
    async def test_test_endpoint_returns_200(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """``POST /api/v1/model-connections/{id}/test`` 端点。"""
        db, admin_id = admin_db
        actor = _actor(admin_id, ["ADMIN"])
        app = self._build_app(db, secret_service, actor=actor)
        client = TestClient(app)

        create = client.post(
            "/api/v1/model-connections",
            json={
                "name": "test-endpoint",
                "provider": "openai",
                "model_id": "gpt-4o",
                "api_base": "https://api.openai.com/v1",
                "credential_type": "api_key",
                "credential_value": _CRED_OPENAI,
                "idempotency_key": "http-test-1",
            },
        )
        assert create.status_code == 201
        cid = create.json()["id"]

        test_resp = client.post(f"/api/v1/model-connections/{cid}/test")
        assert test_resp.status_code == 200
        body = test_resp.json()
        assert body["ok"] is True
        assert body["status"] == "VERIFIED"
        assert body["connection_id"] == cid


# --------------------------------------------------------------------------- #
# ensure_schema 工具函数
# --------------------------------------------------------------------------- #


class TestEnsureSchema:
    """``ensure_schema`` 幂等创建 ``model_connections`` 表。"""

    @pytest.mark.asyncio
    async def test_ensure_schema_idempotent(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        database = Database(settings)
        await database.initialize()
        try:
            await ensure_schema(database)
            await ensure_schema(database)  # 二次调用不抛异常
        finally:
            await database.close()
