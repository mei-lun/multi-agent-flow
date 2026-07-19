"""TASK-039 集成测试：模型连接分层验证（verify_connection）。

验收标准覆盖：

1. ``verify_connection`` 分层验证（config→credential→network→model）。
2. 任一层失败时不继续后续层，后继层标 SKIP。
3. probe 发送轻量请求验证可达性（通过 Mock Adapter 模拟，不依赖真实网络）。
4. 凭据明文不返回给调用方，不进入 ``VerificationResult`` 任何字段。
5. 权限：``read`` ``model_connections``（ADMIN/DESIGNER/OBSERVER 可调用；
   未认证拒绝）。
6. 状态更新：``overall_passed=True`` → ``VERIFIED``；否则 ``ERROR``；
   不递增 ``version_no``。
7. 结果包含耗时（``latency_ms``）和脱敏错误（不含 ``api_key``/``token``）。

测试范围：
- ``apps/server/src/maf_server/modules/model_connections/service.py``（verify_connection）；
- ``apps/server/src/maf_server/gateway/model/probe.py``（ModelProbeService）；
- ``apps/server/src/maf_server/gateway/model/adapters.py``（ProviderAdapterFactory）；
- ``apps/server/src/maf_server/gateway/secrets/local_service.py``（SecretService）。

不测试：真实模型推理调用（用 Mock Adapter 模拟 probe/list_models）。
"""

from __future__ import annotations

import json
import os
import secrets as _secrets
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio

from maf_contracts.common import ActorContext
from maf_contracts.model import (
    CanonicalMessage,
    ModelUsage,
    UnifiedModelRequest,
    UnifiedModelResponse,
)
from maf_domain.errors import (
    NotFoundError,
    PermissionDeniedError,
)
from maf_server.config import ServerSettings
from maf_server.core.database import Database
from maf_server.core.events import init_outbox_schema
from maf_server.core.secrets import MASTER_KEY_SIZE_BYTES
from maf_server.gateway.model.adapters import ProviderAdapterFactory
from maf_server.gateway.model.probe import (
    LayerResult,
    ModelProbeService,
    ProbeResult,
    VerificationResult,
)
from maf_server.gateway.secrets.aes_gcm_store import AesGcmFileStore
from maf_server.gateway.secrets.local_service import LocalSecretService
from maf_server.modules.iam.repository import init_schema as init_iam_schema
from maf_server.modules.iam.service import seed_local_user
from maf_server.modules.model_connections.repository import (
    init_schema as init_mc_schema,
)
from maf_server.modules.model_connections.service import (
    ModelConnectionServiceImpl,
)

_SECRET_PLAINTEXT = "test-secret-for-model-probe-task-039"
_TEST_PASSWORD = "probe-correct-horse-battery-staple"
_ORG_ID = "org-probe-001"

#: 凭据明文样本（足够长，前缀满足 ``sk-`` 约定）。
_CRED_OPENAI = "sk-OPENAI-task039-FAKE-SECRET-1234567890"
_CRED_OPENAI_SHORT = "sk-short"  # 长度不足 8，触发凭据格式失败


# --------------------------------------------------------------------------- #
# 可配置 Mock Adapter：模拟 probe/list_models 行为
# --------------------------------------------------------------------------- #


class _ConfigurableMockAdapter:
    """测试用可配置 Mock Adapter，模拟 probe/list_models 不同行为。

    通过 ``probe_ok``/``models``/``latency_ms``/``probe_error`` 控制返回值，
    不发起任何网络调用。``connection_config`` 中的 ``api_key`` 仅用于验证
    凭据注入路径，绝不进入响应或异常。
    """

    adapter_type: str = "mock"

    def __init__(
        self,
        *,
        probe_ok: bool = True,
        models: list[str] | None = None,
        latency_ms: int = 10,
        probe_error: dict[str, Any] | None = None,
        list_raises: bool = False,
        connection_config: dict[str, Any] | None = None,
    ) -> None:
        self._probe_ok = probe_ok
        self._models = list(models) if models is not None else []
        self._latency_ms = latency_ms
        self._probe_error = probe_error
        self._list_raises = list_raises
        self._connection_config = (
            dict(connection_config) if connection_config else {}
        )
        self.probe_count = 0
        self.list_count = 0

    async def probe(self, connection: dict[str, Any]) -> dict[str, Any]:
        self.probe_count += 1
        result: dict[str, Any] = {
            "ok": self._probe_ok,
            "provider": self.adapter_type,
            "latency_ms": self._latency_ms,
        }
        if not self._probe_ok and self._probe_error is not None:
            result["error"] = self._probe_error
        elif not self._probe_ok:
            result["error"] = {
                "code": "CONNECT_FAILED",
                "category": "server",
                "retryable": True,
                "message": "simulated unreachable",
            }
        return result

    async def list_models(
        self, connection: dict[str, Any]
    ) -> list[dict[str, Any]]:
        self.list_count += 1
        if self._list_raises:
            raise RuntimeError("list_models simulated failure")
        return [{"name": m, "context_window": 4096} for m in self._models]

    async def invoke(
        self,
        connection: dict[str, Any],
        model_name: str,
        request: UnifiedModelRequest,
    ) -> UnifiedModelResponse:
        return UnifiedModelResponse(
            call_id="mock",
            status="COMPLETED",
            model_profile_id=model_name,
            provider_request_id=None,
            message=CanonicalMessage(role="assistant", content="mock"),
            tool_calls=[],
            usage=ModelUsage(
                input_tokens=1,
                output_tokens=1,
                cached_input_tokens=0,
                estimated_cost="0",
                currency="USD",
            ),
            latency_ms=0,
            finish_reason="stop",
            error=None,
        )

    async def stream(
        self,
        connection: dict[str, Any],
        model_name: str,
        request: UnifiedModelRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        yield {"delta": {"content": "mock"}}

    def normalize_error(self, error: Exception) -> dict[str, Any]:
        return {
            "code": "MOCK_ERROR",
            "category": "client",
            "retryable": False,
            "message": "mock error",
        }


def _make_factory(adapter: _ConfigurableMockAdapter) -> ProviderAdapterFactory:
    """构造注册了 mock adapter 的工厂（覆盖 openai/local 路由）。"""
    factory = ProviderAdapterFactory()
    # Preserve specialized test adapters (for example adapters whose probe
    # deliberately raises) while creating a fresh instance per request.
    adapter_type = type(adapter)

    def build(cfg: dict[str, Any]) -> _ConfigurableMockAdapter:
        return adapter_type(
            probe_ok=adapter._probe_ok,
            models=adapter._models,
            latency_ms=adapter._latency_ms,
            probe_error=adapter._probe_error,
            list_raises=adapter._list_raises,
            connection_config=cfg,
        )

    factory.register_adapter("openai", build)
    factory.register_adapter("local", build)
    return factory


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
    """基于 AES-GCM 文件后端的 ``LocalSecretService``。"""
    master_key = _secrets.token_bytes(MASTER_KEY_SIZE_BYTES)
    store = AesGcmFileStore(
        master_key=master_key,
        storage_dir=tmp_path / "secrets",
        organization_id=_ORG_ID,
    )
    return LocalSecretService(primary=store)


def _actor(
    user_id: str, roles: list[str], trace_id: str = "probe-trace"
) -> ActorContext:
    return ActorContext(
        user_id=user_id,
        organization_id=_ORG_ID,
        permission_keys=roles,
        trace_id=trace_id,
    )


def _make_service(
    db: Database,
    secret_service: LocalSecretService,
    *,
    adapter: _ConfigurableMockAdapter | None = None,
) -> ModelConnectionServiceImpl:
    """构造 ``ModelConnectionServiceImpl``，注入 mock adapter 工厂。

    ``adapter`` 控制 probe/list_models 行为；默认返回 probe ok=True，
    list_models 包含 ``gpt-4o``。
    """
    if adapter is None:
        adapter = _ConfigurableMockAdapter(
            probe_ok=True, models=["gpt-4o", "gpt-4o-mini"]
        )
    factory = _make_factory(adapter)
    probe_service = ModelProbeService(
        factory=factory,
        secret_service=secret_service,
        repository=None,
    )
    return ModelConnectionServiceImpl(
        db,
        secret_service=secret_service,
        adapter_factory=factory,
        probe_service=probe_service,
    )


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


def _layer_by_name(
    result: VerificationResult, name: str
) -> LayerResult:
    """从 VerificationResult 取出指定名称的层结果。"""
    for layer in result["layers"]:
        if layer["layer"] == name:
            return layer
    raise AssertionError(f"未找到层 {name!r}，实际 layers={[l['layer'] for l in result['layers']]}")


# --------------------------------------------------------------------------- #
# 验收 1：四层验证全通过
# --------------------------------------------------------------------------- #


class TestAllLayersPass:
    """``verify_connection`` 四层全部通过。"""

    @pytest.mark.asyncio
    async def test_all_pass_returns_overall_passed(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor)
        result = await service.verify_connection(cid, actor=actor)

        assert result["connection_id"] == cid
        assert result["overall_passed"] is True
        assert "verified_at" in result
        # 4 层全部执行，无 SKIP
        assert len(result["layers"]) == 4
        layer_names = [l["layer"] for l in result["layers"]]
        assert layer_names == ["config", "credential", "network", "model"]
        for layer in result["layers"]:
            assert layer["passed"] is True, (
                f"层 {layer['layer']} 未通过：{layer}"
            )
            assert layer["error"] is None

    @pytest.mark.asyncio
    async def test_config_layer_details_contain_provider_model_id(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor, model_id="gpt-4o")
        result = await service.verify_connection(cid, actor=actor)
        config_layer = _layer_by_name(result, "config")
        assert config_layer["details"]["provider"] == "openai"
        assert config_layer["details"]["model_id"] == "gpt-4o"
        # api_base 脱敏：保留 scheme+host，移除 path
        assert config_layer["details"]["api_base"] == "https://api.openai.com"

    @pytest.mark.asyncio
    async def test_network_layer_details_contain_latency(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        adapter = _ConfigurableMockAdapter(
            probe_ok=True, models=["gpt-4o"], latency_ms=42
        )
        service = _make_service(db, secret_service, adapter=adapter)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor)
        result = await service.verify_connection(cid, actor=actor)
        network_layer = _layer_by_name(result, "network")
        assert network_layer["passed"] is True
        assert network_layer["details"]["latency_ms"] == 42
        assert network_layer["details"]["provider"] == "openai"

    @pytest.mark.asyncio
    async def test_model_layer_details_contain_available_models(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        adapter = _ConfigurableMockAdapter(
            probe_ok=True, models=["gpt-4o", "gpt-4o-mini"]
        )
        service = _make_service(db, secret_service, adapter=adapter)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor, model_id="gpt-4o")
        result = await service.verify_connection(cid, actor=actor)
        model_layer = _layer_by_name(result, "model")
        assert model_layer["passed"] is True
        assert model_layer["details"]["model_id"] == "gpt-4o"
        assert "gpt-4o" in model_layer["details"]["available_models"]
        assert "gpt-4o-mini" in model_layer["details"]["available_models"]

    @pytest.mark.asyncio
    async def test_overall_pass_updates_status_to_verified(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor)
        # 初始状态 UNVERIFIED
        view_before = await service.get_connection(cid, actor=actor)
        assert view_before["status"] == "UNVERIFIED"

        result = await service.verify_connection(cid, actor=actor)
        assert result["overall_passed"] is True

        view_after = await service.get_connection(cid, actor=actor)
        assert view_after["status"] == "VERIFIED"
        # verify 不递增 version_no
        assert view_after["version"] == 1


# --------------------------------------------------------------------------- #
# 验收 2：配置层失败 → 后继层 SKIP
# --------------------------------------------------------------------------- #


class TestConfigLayerFailure:
    """配置层失败时后继层标 SKIP。"""

    @pytest.mark.asyncio
    async def test_invalid_api_base_fails_config_layer(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """``api_base`` 不是合法 URL → config 层失败，后继 SKIP。"""
        db, admin_id = admin_db
        service = _make_service(db, secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(
            service, actor, api_base="not-a-valid-url"
        )
        result = await service.verify_connection(cid, actor=actor)

        assert result["overall_passed"] is False
        config_layer = _layer_by_name(result, "config")
        assert config_layer["passed"] is False
        assert "api_base" in config_layer["error"]

        # 后继三层标 SKIP
        for name in ("credential", "network", "model"):
            layer = _layer_by_name(result, name)
            assert layer["passed"] is False
            assert layer["error"] is not None
            assert "SKIP" in layer["error"]

    @pytest.mark.asyncio
    async def test_config_failure_updates_status_to_error(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(
            service, actor, api_base="not-a-valid-url"
        )
        await service.verify_connection(cid, actor=actor)

        view = await service.get_connection(cid, actor=actor)
        assert view["status"] == "ERROR"
        assert view["version"] == 1


# --------------------------------------------------------------------------- #
# 验收 3：凭据层失败 → 后继层 SKIP
# --------------------------------------------------------------------------- #


class TestCredentialLayerFailure:
    """凭据层失败时后继层标 SKIP。"""

    @pytest.mark.asyncio
    async def test_secret_resolve_failure_fails_credential_layer(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """删除 secret 后 resolve 失败 → credential 层失败，network/model SKIP。"""
        db, admin_id = admin_db
        service = _make_service(db, secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor)

        # 从 DB 读出 secret_id 并删除（模拟 secret 失效）
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT credential_secret_id FROM model_connections WHERE id = ?",
                (cid,),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        secret_id = str(row[0])
        await secret_service.delete(secret_id)

        result = await service.verify_connection(cid, actor=actor)

        assert result["overall_passed"] is False
        config_layer = _layer_by_name(result, "config")
        assert config_layer["passed"] is True  # config 通过

        cred_layer = _layer_by_name(result, "credential")
        assert cred_layer["passed"] is False
        assert cred_layer["error"] is not None
        # 错误消息脱敏（不含 secret_id 明文）
        assert "resolve" in cred_layer["error"].lower() or "secret" in cred_layer["error"].lower()

        # network/model 标 SKIP
        network_layer = _layer_by_name(result, "network")
        assert network_layer["passed"] is False
        assert "SKIP" in (network_layer["error"] or "")

        model_layer = _layer_by_name(result, "model")
        assert model_layer["passed"] is False
        assert "SKIP" in (model_layer["error"] or "")

    @pytest.mark.asyncio
    async def test_no_secret_service_fails_credential_layer(
        self,
        admin_db: tuple[Database, str],
    ) -> None:
        """未注入 SecretService → credential 层失败。"""
        db, admin_id = admin_db
        adapter = _ConfigurableMockAdapter(
            probe_ok=True, models=["gpt-4o"]
        )
        factory = _make_factory(adapter)
        probe_service = ModelProbeService(
            factory=factory,
            secret_service=None,
        )
        service = ModelConnectionServiceImpl(
            db,
            secret_service=None,
            probe_service=probe_service,
        )
        actor = _actor(admin_id, ["ADMIN"])

        # create_connection 需要 SecretService，绕过直接造记录
        # 这里用另一条路径：先在有 secret_service 的实例上创建，
        # 然后用无 secret_service 的实例验证。
        svc_with_secret = ModelConnectionServiceImpl(
            db,
            secret_service=LocalSecretService(
                primary=AesGcmFileStore(
                    master_key=_secrets.token_bytes(MASTER_KEY_SIZE_BYTES),
                    storage_dir=Path("/tmp/probe-no-secret"),
                    organization_id=_ORG_ID,
                )
            ),
        )
        cid = await _create_one(svc_with_secret, actor)

        result = await service.verify_connection(cid, actor=actor)
        cred_layer = _layer_by_name(result, "credential")
        assert cred_layer["passed"] is False
        assert "SecretService" in cred_layer["error"]


# --------------------------------------------------------------------------- #
# 验收 4：网络层失败 → model 层 SKIP
# --------------------------------------------------------------------------- #


class TestNetworkLayerFailure:
    """网络层失败时 model 层标 SKIP。"""

    @pytest.mark.asyncio
    async def test_probe_unreachable_fails_network_layer(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """Adapter.probe 返回 ok=False → network 层失败，model SKIP。"""
        db, admin_id = admin_db
        adapter = _ConfigurableMockAdapter(
            probe_ok=False,
            models=["gpt-4o"],
            probe_error={
                "code": "CONNECT_FAILED",
                "category": "server",
                "retryable": True,
                "message": "simulated unreachable",
            },
        )
        service = _make_service(db, secret_service, adapter=adapter)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor)
        result = await service.verify_connection(cid, actor=actor)

        assert result["overall_passed"] is False

        config_layer = _layer_by_name(result, "config")
        assert config_layer["passed"] is True

        cred_layer = _layer_by_name(result, "credential")
        assert cred_layer["passed"] is True

        network_layer = _layer_by_name(result, "network")
        assert network_layer["passed"] is False
        assert network_layer["error"] is not None
        # 错误消息不含 api_key
        assert "api_key" not in network_layer["error"].lower()
        # 包含 latency
        assert "latency_ms" in network_layer["details"]

        # model 层 SKIP
        model_layer = _layer_by_name(result, "model")
        assert model_layer["passed"] is False
        assert "SKIP" in (model_layer["error"] or "")

    @pytest.mark.asyncio
    async def test_probe_exception_fails_network_layer(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """probe 抛异常 → network 层失败（不传播异常）。"""
        db, admin_id = admin_db

        class _ExplodingAdapter(_ConfigurableMockAdapter):
            async def probe(self, connection):
                raise RuntimeError("api_key=sk-leaked-in-exception boom")

        adapter = _ExplodingAdapter(probe_ok=True, models=["gpt-4o"])
        service = _make_service(db, secret_service, adapter=adapter)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor)
        result = await service.verify_connection(cid, actor=actor)

        network_layer = _layer_by_name(result, "network")
        assert network_layer["passed"] is False
        # 异常 message 含 api_key 字样 → 脱敏为 provider error
        assert "sk-leaked" not in (network_layer["error"] or "")
        assert "api_key" not in (network_layer["error"] or "").lower()


# --------------------------------------------------------------------------- #
# 验收 5：模型不在列表中
# --------------------------------------------------------------------------- #


class TestModelNotInList:
    """``model_id`` 不在 provider 返回的模型列表中。"""

    @pytest.mark.asyncio
    async def test_model_not_in_available_models(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        adapter = _ConfigurableMockAdapter(
            probe_ok=True,
            models=["gpt-4o", "gpt-4o-mini"],
        )
        service = _make_service(db, secret_service, adapter=adapter)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor, model_id="claude-3-opus")
        result = await service.verify_connection(cid, actor=actor)

        assert result["overall_passed"] is False

        model_layer = _layer_by_name(result, "model")
        assert model_layer["passed"] is False
        assert "claude-3-opus" in (model_layer["error"] or "")
        assert "gpt-4o" in model_layer["details"]["available_models"]
        assert "claude-3-opus" not in model_layer["details"]["available_models"]

    @pytest.mark.asyncio
    async def test_empty_model_list_passes_leniently(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """provider 返回空模型列表时宽松通过（部分 provider 无 list 端点）。"""
        db, admin_id = admin_db
        adapter = _ConfigurableMockAdapter(
            probe_ok=True,
            models=[],  # 空列表
        )
        service = _make_service(db, secret_service, adapter=adapter)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor, model_id="gpt-4o")
        result = await service.verify_connection(cid, actor=actor)

        model_layer = _layer_by_name(result, "model")
        assert model_layer["passed"] is True
        assert model_layer["details"]["available_models"] == []
        assert "宽松通过" in model_layer["details"].get("note", "")

    @pytest.mark.asyncio
    async def test_list_models_exception_passes_leniently(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """``list_models`` 抛异常时宽松通过。"""
        db, admin_id = admin_db
        adapter = _ConfigurableMockAdapter(
            probe_ok=True,
            models=["gpt-4o"],
            list_raises=True,
        )
        service = _make_service(db, secret_service, adapter=adapter)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor)
        result = await service.verify_connection(cid, actor=actor)

        model_layer = _layer_by_name(result, "model")
        assert model_layer["passed"] is True
        assert "宽松通过" in model_layer["details"].get("note", "")


# --------------------------------------------------------------------------- #
# 验收 6：凭据明文不返回给调用方
# --------------------------------------------------------------------------- #


class TestCredentialNotLeaked:
    """凭据明文绝不进入 ``VerificationResult`` 任何字段。"""

    @pytest.mark.asyncio
    async def test_plaintext_not_in_result(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """``VerificationResult`` 序列化后不含凭据明文。"""
        db, admin_id = admin_db
        service = _make_service(db, secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor, credential_value=_CRED_OPENAI)
        result = await service.verify_connection(cid, actor=actor)

        # 序列化整个 result，检查明文不出现
        result_str = json.dumps(result, default=str, ensure_ascii=False)
        assert _CRED_OPENAI not in result_str
        # api_key 字样也不应作为明文出现
        assert "sk-OPENAI-task039" not in result_str

    @pytest.mark.asyncio
    async def test_plaintext_not_in_db_or_outbox(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """verify 后明文不进入 ``model_connections``/``outbox_events`` 任何列。"""
        db, admin_id = admin_db
        service = _make_service(db, secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor, credential_value=_CRED_OPENAI)
        await service.verify_connection(cid, actor=actor)

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
    async def test_error_messages_redacted(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """失败层 error 消息不含 ``api_key``/``token``/``bearer`` 字样。"""
        db, admin_id = admin_db

        class _LeakyAdapter(_ConfigurableMockAdapter):
            async def probe(self, connection):
                raise RuntimeError(
                    "Authorization: Bearer sk-leaked-token boom"
                )

        adapter = _LeakyAdapter(probe_ok=True, models=["gpt-4o"])
        service = _make_service(db, secret_service, adapter=adapter)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor)
        result = await service.verify_connection(cid, actor=actor)

        network_layer = _layer_by_name(result, "network")
        assert network_layer["passed"] is False
        assert "sk-leaked" not in (network_layer["error"] or "")
        assert "bearer" not in (network_layer["error"] or "").lower()
        assert "token" not in (network_layer["error"] or "").lower()


# --------------------------------------------------------------------------- #
# 验收 7：权限检查
# --------------------------------------------------------------------------- #


class TestPermissionEnforcement:
    """``verify_connection`` 权限校验。"""

    @pytest.mark.asyncio
    async def test_admin_can_verify(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor)
        result = await service.verify_connection(cid, actor=actor)
        assert result["overall_passed"] is True

    @pytest.mark.asyncio
    async def test_observer_can_verify(
        self,
        admin_db: tuple[Database, str],
        observer_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """``verify_connection`` 是 read 操作，OBSERVER 可调用。"""
        db, admin_id = admin_db
        _, observer_id = observer_db
        service = _make_service(db, secret_service)

        admin_actor = _actor(admin_id, ["ADMIN"])
        cid = await _create_one(service, admin_actor)

        observer_actor = _actor(observer_id, ["OBSERVER"])
        result = await service.verify_connection(cid, actor=observer_actor)
        assert result["overall_passed"] is True

    @pytest.mark.asyncio
    async def test_unauthenticated_denied(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service)

        bad_actor = ActorContext(
            user_id="",
            organization_id=_ORG_ID,
            permission_keys=["ADMIN"],
            trace_id="t",
        )
        with pytest.raises(PermissionDeniedError):
            await service.verify_connection("any-id", actor=bad_actor)

    @pytest.mark.asyncio
    async def test_nonexistent_connection_raises_not_found(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        with pytest.raises(NotFoundError):
            await service.verify_connection("nonexistent-id", actor=actor)


# --------------------------------------------------------------------------- #
# 补充：SKIP 机制与层序
# --------------------------------------------------------------------------- #


class TestSkipMechanism:
    """前级失败后继层 SKIP 标记正确。"""

    @pytest.mark.asyncio
    async def test_config_fail_only_config_executed_others_skip(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        adapter = _ConfigurableMockAdapter(probe_ok=True, models=["gpt-4o"])
        service = _make_service(db, secret_service, adapter=adapter)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor, api_base="bad-url")
        result = await service.verify_connection(cid, actor=actor)

        # config 失败 → credential/network/model 均 SKIP
        layers = result["layers"]
        assert len(layers) == 4
        assert layers[0]["layer"] == "config"
        assert layers[0]["passed"] is False
        for layer in layers[1:]:
            assert layer["passed"] is False
            assert layer["error"] is not None
            assert layer["error"].startswith("SKIP:")

    @pytest.mark.asyncio
    async def test_network_fail_only_model_skipped(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        """network 失败时只有 model SKIP，config/credential/network 已执行。"""
        db, admin_id = admin_db
        adapter = _ConfigurableMockAdapter(probe_ok=False, models=["gpt-4o"])
        service = _make_service(db, secret_service, adapter=adapter)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor)
        result = await service.verify_connection(cid, actor=actor)

        layers = result["layers"]
        assert len(layers) == 4
        # config & credential 通过
        assert layers[0]["passed"] is True  # config
        assert layers[1]["passed"] is True  # credential
        # network 失败
        assert layers[2]["layer"] == "network"
        assert layers[2]["passed"] is False
        # model SKIP
        assert layers[3]["layer"] == "model"
        assert layers[3]["passed"] is False
        assert "SKIP" in (layers[3]["error"] or "")


# --------------------------------------------------------------------------- #
# 补充：TypedDict 结构
# --------------------------------------------------------------------------- #


class TestTypedDictStructure:
    """``VerificationResult``/``LayerResult``/``ProbeResult`` 结构校验。"""

    @pytest.mark.asyncio
    async def test_verification_result_has_required_fields(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor)
        result = await service.verify_connection(cid, actor=actor)

        assert "connection_id" in result
        assert "verified_at" in result
        assert "overall_passed" in result
        assert "layers" in result
        assert isinstance(result["layers"], list)
        assert len(result["layers"]) == 4

    @pytest.mark.asyncio
    async def test_layer_result_has_required_fields(
        self,
        admin_db: tuple[Database, str],
        secret_service: LocalSecretService,
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db, secret_service)
        actor = _actor(admin_id, ["ADMIN"])

        cid = await _create_one(service, actor)
        result = await service.verify_connection(cid, actor=actor)

        for layer in result["layers"]:
            assert "layer" in layer
            assert "passed" in layer
            assert "details" in layer
            assert "error" in layer
            assert isinstance(layer["details"], dict)
            assert isinstance(layer["passed"], bool)

    def test_probe_result_typeddict_defined(self) -> None:
        """``ProbeResult`` TypedDict 在模块可导入。"""
        # 仅校验类型可访问与字段定义
        assert ProbeResult is not None
        # TypedDict 的 __annotations__ 应包含 4 个字段
        annotations = ProbeResult.__annotations__
        for field in ("reachable", "latency_ms", "available_models", "error"):
            assert field in annotations, f"ProbeResult 缺少字段 {field}"

    def test_layer_result_typeddict_defined(self) -> None:
        assert LayerResult is not None
        annotations = LayerResult.__annotations__
        for field in ("layer", "passed", "details", "error"):
            assert field in annotations, f"LayerResult 缺少字段 {field}"

    def test_verification_result_typeddict_defined(self) -> None:
        assert VerificationResult is not None
        annotations = VerificationResult.__annotations__
        for field in ("connection_id", "verified_at", "overall_passed", "layers"):
            assert field in annotations, f"VerificationResult 缺少字段 {field}"
