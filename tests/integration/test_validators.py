"""TASK-080 集成测试：确定性 Validator 框架。

验收标准覆盖（对应 TASK-080 文档与任务描述）：

1. **Validator Protocol**：``supports``/``validate`` 接口定义。
2. **ValidationResult / ValidationIssue**：dataclass 字段（status/issues/
   validator_name/validated_at 与 severity/code/message/path）。
3. **内置 Validator**：``JsonSchemaValidator``/``SizeLimitValidator``/
   ``HashIntegrityValidator``。
4. **ValidatorRegistry**：``register``/``validate_artifact``，按 artifact_type
   选择 Validator。
5. **ERROR ≠ PASS**：Validator 返回 ``ERROR`` 时整体结果必须为失败（不能降级为
   PASS）。
6. **ReviewService**：``submit_review``/``get_review``/``list_reviews``。
7. **数据库表**：``artifact_reviews``（id、artifact_id、status、validator_results
   TEXT JSON、reviewer、reviewed_at、version_no）。
8. **权限检查**：``submit_review`` 需 ``write reviews``、``get_review``/
   ``list_reviews`` 需 ``read reviews``。
9. **不破坏 TASK-078/079**：复用 ``ArtifactService`` 上传 artifact、
   ``ArtifactSchemaService`` 注册 schema。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from maf_contracts.common import ActorContext
from maf_domain.errors import (
    ArgumentError,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedOperationError,
)
from maf_policy import CasbinPermissionService
from maf_server.config import ServerSettings
from maf_server.core.artifact_store import LocalArtifactFileStore
from maf_server.core.database import Database
from maf_server.core.events import (
    SqliteOutboxRepository,
    init_outbox_schema,
)
from maf_server.modules.artifacts.repository import (
    SqliteArtifactRepository,
    init_schema as init_artifact_schema,
)
from maf_server.modules.artifacts.service import (
    ArtifactSchemaServiceImpl,
    ArtifactServiceImpl,
    HashIntegrityValidator,
    JsonSchemaValidator,
    SizeLimitValidator,
    Validator,
    ValidatorIssue,
    ValidatorRegistry,
    ValidatorResult,
    ValidatorStatus,
    aggregate_review_status,
)
from maf_server.modules.reviews.repository import (
    SqliteArtifactReviewRepository,
    init_artifact_reviews_schema,
)
from maf_server.modules.reviews.service import ArtifactReviewServiceImpl

# packages/artifact_schemas/src 需要在 sys.path 中（pyproject.toml pythonpath 未含），
# 与 tests/integration/test_artifact_lineage.py 一致。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT_SCHEMAS_SRC = _PROJECT_ROOT / "packages" / "artifact_schemas" / "src"
if str(_ARTIFACT_SCHEMAS_SRC) not in sys.path:
    sys.path.insert(0, str(_ARTIFACT_SCHEMAS_SRC))

_SECRET_PLAINTEXT = "test-secret-for-artifact-task-080"


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


def _make_permission_service() -> CasbinPermissionService:
    """构造带 artifact / artifact_schemas / reviews 策略的 CasbinPermissionService。

    DEFAULT_POLICIES 已含 ``("APPROVER", "reviews", ".*")`` 与
    ``("OBSERVER", "*", "read")``；本测试额外追加 OWNER/DESIGNER 的 artifacts 与
    artifact_schemas 读写策略，以及 OWNER 的 reviews 读写策略。
    ADMIN 默认拥有 ``*`` ``.*`` 全权。
    """
    service = CasbinPermissionService()
    service.add_policy("OWNER", "artifacts", "(read|write)")
    service.add_policy("DESIGNER", "artifacts", "(read|write)")
    service.add_policy("APPROVER", "artifacts", "read")
    service.add_policy("OWNER", "artifact_schemas", "(read|write)")
    service.add_policy("DESIGNER", "artifact_schemas", "(read|write)")
    service.add_policy("APPROVER", "artifact_schemas", "read")
    service.add_policy("OWNER", "reviews", "(read|write)")
    service.add_policy("DESIGNER", "reviews", "(read|write)")
    return service


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    """已初始化并建好 artifacts / artifact_schemas / artifact_lineage /
    artifact_reviews / outbox_events 表的 Database。"""
    settings = _make_settings(tmp_path)
    database = Database(settings)
    await database.initialize()
    # 建 artifacts + artifact_schemas + artifact_lineage 表
    async with database.write_connection() as conn:
        await init_artifact_schema(conn)
        # 建 artifact_reviews 表
        await init_artifact_reviews_schema(conn)
    # 建 outbox_events 表
    await init_outbox_schema(database)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def file_store(db: Database) -> LocalArtifactFileStore:
    """基于 ServerSettings.artifact_root 的 LocalArtifactFileStore。"""
    settings = db._settings  # type: ignore[attr-defined]
    return LocalArtifactFileStore(settings.artifact_root)


@pytest_asyncio.fixture
async def artifact_service(
    db: Database, file_store: LocalArtifactFileStore
) -> ArtifactServiceImpl:
    """注入 Database、FileStore、自定义 PermissionService 的 ArtifactServiceImpl。"""
    return ArtifactServiceImpl(
        database=db,
        file_store=file_store,
        permission_service=_make_permission_service(),
    )


@pytest_asyncio.fixture
async def schema_service(
    db: Database, file_store: LocalArtifactFileStore
) -> ArtifactSchemaServiceImpl:
    """注入 Database、FileStore、自定义 PermissionService 的 ArtifactSchemaServiceImpl。"""
    return ArtifactSchemaServiceImpl(
        database=db,
        file_store=file_store,
        permission_service=_make_permission_service(),
    )


@pytest_asyncio.fixture
async def registry(
    db: Database, file_store: LocalArtifactFileStore
) -> ValidatorRegistry:
    """注入 Database、FileStore、自定义 PermissionService 的 ValidatorRegistry。"""
    return ValidatorRegistry(
        database=db,
        file_store=file_store,
        permission_service=_make_permission_service(),
    )


@pytest_asyncio.fixture
async def review_service(db: Database) -> ArtifactReviewServiceImpl:
    """注入 Database、自定义 PermissionService 的 ArtifactReviewServiceImpl。"""
    return ArtifactReviewServiceImpl(
        database=db,
        permission_service=_make_permission_service(),
    )


def _actor(
    user_id: str = "user-admin",
    roles: list[str] | None = None,
    trace_id: str = "validator-trace",
) -> ActorContext:
    """构造测试用 ActorContext。

    ``roles=None`` 时默认 ADMIN；``roles=[]`` 显式表示无角色。
    """
    return ActorContext(
        user_id=user_id,
        organization_id="org-001",
        permission_keys=roles if roles is not None else ["ADMIN"],
        trace_id=trace_id,
    )


def _content(data: str = "hello validator world") -> bytes:
    """测试用内容。"""
    return data.encode("utf-8")


def _json_content(obj: Any) -> bytes:
    """把 Python 对象序列化为 JSON bytes。"""
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _simple_schema() -> dict[str, Any]:
    """构造一个简单的 JSON Schema（要求 ``name`` 必填）。"""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "version": {"type": "integer", "minimum": 1},
        },
        "required": ["name"],
        "additionalProperties": False,
    }


async def _upload(
    service: ArtifactServiceImpl,
    *,
    project_id: str = "proj-001",
    artifact_type: str = "snapshot",
    content: bytes | None = None,
    actor: ActorContext | None = None,
) -> str:
    """上传一个 artifact，返回 artifact_id。"""
    if actor is None:
        actor = _actor(roles=["ADMIN"])
    if content is None:
        content = _content("default-validator-content")
    view = await service.upload_artifact(
        project_id,
        artifact_type,
        content,
        actor_id=actor["user_id"],
        actor=actor,
    )
    return view["id"]


async def _register_schema(
    service: ArtifactSchemaServiceImpl,
    *,
    schema_name: str = "task_payload",
    version: int = 1,
    json_schema: dict[str, Any] | None = None,
    actor: ActorContext | None = None,
) -> None:
    """注册一个 Schema 版本。"""
    if actor is None:
        actor = _actor(roles=["DESIGNER"])
    if json_schema is None:
        json_schema = _simple_schema()
    await service.register_schema(
        schema_name, version, json_schema,
        actor_id=actor["user_id"], actor=actor,
    )


# --------------------------------------------------------------------------- #
# 验收 1：Validator Protocol 与 ValidationResult / ValidationIssue
# --------------------------------------------------------------------------- #


class TestValidatorProtocol:
    """``Validator`` Protocol 与 ``ValidatorResult`` / ``ValidatorIssue`` 测试。"""

    def test_validator_protocol_is_runtime_checkable(self) -> None:
        """``Validator`` 是 ``@runtime_checkable`` Protocol，可 ``isinstance`` 检查。"""
        validator = HashIntegrityValidator()
        assert isinstance(validator, Validator)

    def test_validator_result_fields(self) -> None:
        """``ValidatorResult`` dataclass 含 status/issues/validator_name/validated_at。"""
        result = ValidatorResult(
            status="PASS",
            issues=[],
            validator_name="test_validator",
            validated_at="2026-07-17T00:00:00+00:00",
        )
        assert result.status == "PASS"
        assert result.issues == []
        assert result.validator_name == "test_validator"
        assert result.validated_at == "2026-07-17T00:00:00+00:00"

    def test_validator_issue_fields(self) -> None:
        """``ValidatorIssue`` dataclass 含 severity/code/message/path。"""
        issue = ValidatorIssue(
            severity="ERROR",
            code="test.error",
            message="test message",
            path="$.foo",
        )
        assert issue.severity == "ERROR"
        assert issue.code == "test.error"
        assert issue.message == "test message"
        assert issue.path == "$.foo"

    def test_validator_issue_path_optional(self) -> None:
        """``path`` 默认为 None。"""
        issue = ValidatorIssue(
            severity="WARNING",
            code="test.warn",
            message="warning message",
        )
        assert issue.path is None

    def test_validator_result_to_dict_serializable(self) -> None:
        """``ValidatorResult.to_dict`` 返回 JSON 兼容 dict。"""
        result = ValidatorResult(
            status="FAIL",
            issues=[
                ValidatorIssue(
                    severity="ERROR",
                    code="test.error",
                    message="error",
                    path="$.x",
                )
            ],
            validator_name="test",
            validated_at="2026-07-17T00:00:00+00:00",
        )
        d = result.to_dict()
        # 可 JSON 序列化（存入 artifact_reviews.validator_results）
        json.dumps(d)
        assert d["status"] == "FAIL"
        assert d["validator_name"] == "test"
        assert len(d["issues"]) == 1
        assert d["issues"][0]["code"] == "test.error"


# --------------------------------------------------------------------------- #
# 验收 2：内置 Validator —— HashIntegrityValidator
# --------------------------------------------------------------------------- #


class TestHashIntegrityValidator:
    """``HashIntegrityValidator`` 测试。"""

    @pytest.mark.asyncio
    async def test_pass_when_hash_matches(
        self, artifact_service: ArtifactServiceImpl
    ) -> None:
        """内容 hash 与 artifact.content_hash 一致 → PASS。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("hash-test-content")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        artifact_view = await artifact_service.get_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )

        validator = HashIntegrityValidator()
        result = await validator.validate(artifact_view, content)
        assert result.status == "PASS"
        assert result.issues == []
        assert result.validator_name == "hash_integrity"
        assert result.validated_at  # ISO 非空

    @pytest.mark.asyncio
    async def test_fail_when_hash_mismatches(
        self, artifact_service: ArtifactServiceImpl
    ) -> None:
        """内容 hash 与 artifact.content_hash 不一致 → FAIL。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("original-content")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        artifact_view = await artifact_service.get_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )

        validator = HashIntegrityValidator()
        # 篡改内容
        tampered = _content("tampered-content")
        result = await validator.validate(artifact_view, tampered)
        assert result.status == "FAIL"
        assert len(result.issues) == 1
        assert result.issues[0].severity == "ERROR"
        assert result.issues[0].code == "hash.mismatch"

    def test_supports_all_types_by_default(self) -> None:
        """``artifact_types=None`` 时支持所有类型。"""
        validator = HashIntegrityValidator()
        assert validator.supports("snapshot") is True
        assert validator.supports("code") is True
        assert validator.supports("any_type") is True

    def test_supports_filtered_by_artifact_types(self) -> None:
        """``artifact_types`` 白名单过滤。"""
        validator = HashIntegrityValidator(
            artifact_types=("snapshot", "report")
        )
        assert validator.supports("snapshot") is True
        assert validator.supports("report") is True
        assert validator.supports("code") is False

    def test_custom_name(self) -> None:
        """可注入自定义 name。"""
        validator = HashIntegrityValidator(name="custom_hash")
        assert validator.name == "custom_hash"


# --------------------------------------------------------------------------- #
# 验收 2（续）：内置 Validator —— SizeLimitValidator
# --------------------------------------------------------------------------- #


class TestSizeLimitValidator:
    """``SizeLimitValidator`` 测试。"""

    @pytest.mark.asyncio
    async def test_pass_when_under_limit(
        self, artifact_service: ArtifactServiceImpl
    ) -> None:
        """内容大小未超上限 → PASS。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("small")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        artifact_view = await artifact_service.get_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )

        validator = SizeLimitValidator(max_size_bytes=1024)
        result = await validator.validate(artifact_view, content)
        assert result.status == "PASS"

    @pytest.mark.asyncio
    async def test_fail_when_over_limit(
        self, artifact_service: ArtifactServiceImpl
    ) -> None:
        """内容大小超过上限 → FAIL + ERROR 级 issue。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("x" * 200)
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        artifact_view = await artifact_service.get_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )

        validator = SizeLimitValidator(max_size_bytes=100)
        result = await validator.validate(artifact_view, content)
        assert result.status == "FAIL"
        codes = [i.code for i in result.issues]
        assert "size.exceeded" in codes
        # 超限的 issue 必须是 ERROR 级（阻断项）
        exceeded = [i for i in result.issues if i.code == "size.exceeded"][0]
        assert exceeded.severity == "ERROR"

    @pytest.mark.asyncio
    async def test_warning_when_metadata_mismatches(
        self, artifact_service: ArtifactServiceImpl
    ) -> None:
        """artifact.size_bytes 与实际长度不一致 → WARNING（不阻断 PASS）。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("metadata-mismatch-test")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        artifact_view = await artifact_service.get_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )
        # 篡改 artifact_view 的 size_bytes
        tampered_view = dict(artifact_view)
        tampered_view["size_bytes"] = 99999

        validator = SizeLimitValidator(max_size_bytes=1024 * 1024)
        result = await validator.validate(tampered_view, content)
        # 只有 WARNING，没有 ERROR → 仍 PASS
        assert result.status == "PASS"
        codes = [i.code for i in result.issues]
        assert "size.metadata_mismatch" in codes
        warning = [i for i in result.issues if i.code == "size.metadata_mismatch"][0]
        assert warning.severity == "WARNING"

    def test_invalid_max_size_rejected(self) -> None:
        """非法 max_size_bytes 抛 ValueError。"""
        with pytest.raises(ValueError):
            SizeLimitValidator(max_size_bytes=-1)
        with pytest.raises(ValueError):
            SizeLimitValidator(max_size_bytes=True)  # type: ignore[arg-type]

    def test_supports_filtered(self) -> None:
        """``artifact_types`` 白名单过滤。"""
        validator = SizeLimitValidator(
            max_size_bytes=100, artifact_types=("snapshot",)
        )
        assert validator.supports("snapshot") is True
        assert validator.supports("code") is False


# --------------------------------------------------------------------------- #
# 验收 2（续）：内置 Validator —— JsonSchemaValidator
# --------------------------------------------------------------------------- #


class TestJsonSchemaValidator:
    """``JsonSchemaValidator`` 测试。"""

    @pytest.mark.asyncio
    async def test_pass_when_content_matches_schema(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
        db: Database,
    ) -> None:
        """内容符合 Schema → PASS。"""
        actor = _actor(roles=["DESIGNER"])
        await _register_schema(schema_service, actor=actor)
        content = _json_content({"name": "task-1", "version": 1})
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        artifact_view = await artifact_service.get_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )

        validator = JsonSchemaValidator(
            db, "task_payload", 1, artifact_types=("snapshot",)
        )
        result = await validator.validate(artifact_view, content)
        assert result.status == "PASS"
        assert result.issues == []
        assert result.validator_name == "json_schema:task_payload:v1"

    @pytest.mark.asyncio
    async def test_fail_when_content_violates_schema(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
        db: Database,
    ) -> None:
        """内容违反 Schema（缺 required 字段）→ FAIL + ERROR 级 issue。"""
        actor = _actor(roles=["DESIGNER"])
        await _register_schema(schema_service, actor=actor)
        # 缺 name 字段
        content = _json_content({"version": 1})
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        artifact_view = await artifact_service.get_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )

        validator = JsonSchemaValidator(db, "task_payload", 1)
        result = await validator.validate(artifact_view, content)
        assert result.status == "FAIL"
        assert len(result.issues) >= 1
        # 所有 issue 都是 ERROR 级
        assert all(i.severity == "ERROR" for i in result.issues)
        # required 错误路径解析为 $.name
        paths = [i.path for i in result.issues]
        assert any("name" in (p or "") for p in paths)

    @pytest.mark.asyncio
    async def test_fail_when_content_not_json(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
        db: Database,
    ) -> None:
        """内容非 JSON → FAIL。"""
        actor = _actor(roles=["DESIGNER"])
        await _register_schema(schema_service, actor=actor)
        content = b"not a json document"
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        artifact_view = await artifact_service.get_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )

        validator = JsonSchemaValidator(db, "task_payload", 1)
        result = await validator.validate(artifact_view, content)
        assert result.status == "FAIL"
        codes = [i.code for i in result.issues]
        assert "json_schema.invalid_json" in codes

    @pytest.mark.asyncio
    async def test_error_when_schema_not_found(
        self,
        artifact_service: ArtifactServiceImpl,
        db: Database,
    ) -> None:
        """Schema 不存在 → ERROR（Validator 自身出错，必须视为失败）。"""
        actor = _actor(roles=["ADMIN"])
        content = _json_content({"name": "x"})
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        artifact_view = await artifact_service.get_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )

        validator = JsonSchemaValidator(db, "nonexistent", 1)
        result = await validator.validate(artifact_view, content)
        # schema 不存在是 Validator 自身无法完成校验，返回 ERROR
        assert result.status == "ERROR"
        codes = [i.code for i in result.issues]
        assert "json_schema.not_found" in codes

    def test_supports_filtered(
        self, db: Database
    ) -> None:
        """``artifact_types`` 白名单过滤。"""
        validator = JsonSchemaValidator(
            db, "task_payload", 1, artifact_types=("snapshot", "report")
        )
        assert validator.supports("snapshot") is True
        assert validator.supports("report") is True
        assert validator.supports("code") is False

    @pytest.mark.asyncio
    async def test_deterministic_results(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
        db: Database,
    ) -> None:
        """相同 (schema, content) 多次校验结果一致（确定性）。"""
        actor = _actor(roles=["DESIGNER"])
        await _register_schema(schema_service, actor=actor)
        content = _json_content({"version": 1})  # 缺 name
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        artifact_view = await artifact_service.get_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )

        validator = JsonSchemaValidator(db, "task_payload", 1)
        r1 = await validator.validate(artifact_view, content)
        r2 = await validator.validate(artifact_view, content)
        # 状态、issue code、path 一致（validated_at 可能不同）
        assert r1.status == r2.status
        assert [i.code for i in r1.issues] == [i.code for i in r2.issues]
        assert [i.path for i in r1.issues] == [i.path for i in r2.issues]


# --------------------------------------------------------------------------- #
# 验收 3：ValidatorRegistry
# --------------------------------------------------------------------------- #


class TestValidatorRegistry:
    """``ValidatorRegistry`` 注册与批量验证测试。"""

    def test_register_validator(self, registry: ValidatorRegistry) -> None:
        """``register`` 添加 Validator 到注册表。"""
        v = HashIntegrityValidator()
        registry.register(v)
        assert v in registry.list_validators()

    def test_register_dedup_by_name(self, registry: ValidatorRegistry) -> None:
        """相同 name 的 Validator 重复注册被忽略（幂等）。"""
        v1 = HashIntegrityValidator(name="dup")
        v2 = HashIntegrityValidator(name="dup")
        registry.register(v1)
        registry.register(v2)
        assert len(registry.list_validators()) == 1

    @pytest.mark.asyncio
    async def test_validate_artifact_runs_all_supported(
        self,
        artifact_service: ArtifactServiceImpl,
        registry: ValidatorRegistry,
    ) -> None:
        """``validate_artifact`` 对 artifact 运行所有 supports 的 Validator。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("registry-test")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        registry.register(HashIntegrityValidator())
        registry.register(SizeLimitValidator(max_size_bytes=1024))
        # 只支持 "code" 类型，不会运行
        registry.register(
            SizeLimitValidator(
                max_size_bytes=1, artifact_types=("code",), name="size_for_code"
            )
        )

        results = await registry.validate_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )
        # 2 个 Validator 运行了（hash_integrity + size_limit），size_for_code 被跳过
        names = {r.validator_name for r in results}
        assert names == {"hash_integrity", "size_limit"}
        # 都 PASS
        assert all(r.status == "PASS" for r in results)

    @pytest.mark.asyncio
    async def test_validate_artifact_returns_fail_when_any_fail(
        self,
        artifact_service: ArtifactServiceImpl,
        registry: ValidatorRegistry,
    ) -> None:
        """任一 Validator FAIL 时，结果列表含 FAIL（由调用方或汇总函数判定）。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("x" * 200)
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        registry.register(HashIntegrityValidator())
        # size_limit 设为 100，content 是 200 字节 → FAIL
        registry.register(SizeLimitValidator(max_size_bytes=100))

        results = await registry.validate_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )
        statuses = {r.status for r in results}
        assert "FAIL" in statuses
        # aggregate_review_status 汇总为 FAIL
        assert aggregate_review_status(results) == "FAIL"

    @pytest.mark.asyncio
    async def test_validate_artifact_not_found(
        self, registry: ValidatorRegistry
    ) -> None:
        """不存在的 artifact 抛 NotFoundError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(NotFoundError):
            await registry.validate_artifact(
                "nonexistent-id", actor_id=actor["user_id"], actor=actor
            )

    @pytest.mark.asyncio
    async def test_validate_artifact_permission_denied(
        self,
        artifact_service: ArtifactServiceImpl,
        registry: ValidatorRegistry,
    ) -> None:
        """无 read artifacts 权限的 actor 被拒绝。"""
        actor = _actor(roles=["OBSERVER"], user_id="observer")
        # OBSERVER 默认有 read 权限（DEFAULT_POLICIES 中 ("OBSERVER", "*", "read")）
        # 用无角色用户测试
        nobody = _actor(roles=[], user_id="nobody")
        admin = _actor(roles=["ADMIN"])
        content = _content("perm-test")
        artifact_id = await _upload(
            artifact_service, content=content, actor=admin
        )

        with pytest.raises(PermissionDeniedError):
            await registry.validate_artifact(
                artifact_id, actor_id=nobody["user_id"], actor=nobody
            )

    @pytest.mark.asyncio
    async def test_validator_exception_becomes_error(
        self,
        artifact_service: ArtifactServiceImpl,
        registry: ValidatorRegistry,
    ) -> None:
        """Validator 抛异常时归一为 ERROR 结果（不冒泡，但 ERROR 视为失败）。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("exception-test")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        class _BoomValidator:
            """总是抛异常的 Validator。"""

            @property
            def name(self) -> str:
                return "boom"

            def supports(self, artifact_type: str) -> bool:
                return True

            async def validate(self, artifact, content) -> ValidatorResult:
                raise RuntimeError("boom!")

        registry.register(_BoomValidator())
        results = await registry.validate_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )
        assert len(results) == 1
        assert results[0].status == "ERROR"
        assert results[0].validator_name == "boom"
        codes = [i.code for i in results[0].issues]
        assert "validator.exception" in codes


# --------------------------------------------------------------------------- #
# 验收 4：ERROR ≠ PASS
# --------------------------------------------------------------------------- #


class TestErrorNotPass:
    """``ERROR`` 状态不被视为 ``PASS``（验收标准 1）。"""

    def test_aggregate_all_pass(self) -> None:
        """全部 PASS → 整体 PASS。"""
        results = [
            ValidatorResult(status="PASS", validator_name="a", validated_at=""),
            ValidatorResult(status="PASS", validator_name="b", validated_at=""),
        ]
        assert aggregate_review_status(results) == "PASS"

    def test_aggregate_one_fail_makes_fail(self) -> None:
        """任一 FAIL → 整体 FAIL（无 ERROR 时）。"""
        results = [
            ValidatorResult(status="PASS", validator_name="a", validated_at=""),
            ValidatorResult(status="FAIL", validator_name="b", validated_at=""),
        ]
        assert aggregate_review_status(results) == "FAIL"

    def test_aggregate_error_overrides_fail(self) -> None:
        """任一 ERROR → 整体 ERROR（优先级高于 FAIL）。"""
        results = [
            ValidatorResult(status="FAIL", validator_name="a", validated_at=""),
            ValidatorResult(status="ERROR", validator_name="b", validated_at=""),
        ]
        assert aggregate_review_status(results) == "ERROR"

    def test_aggregate_error_not_pass(self) -> None:
        """ERROR 不能降级为 PASS（即使有 PASS 结果）。"""
        results = [
            ValidatorResult(status="PASS", validator_name="a", validated_at=""),
            ValidatorResult(status="ERROR", validator_name="b", validated_at=""),
        ]
        # 关键断言：ERROR ≠ PASS
        assert aggregate_review_status(results) != "PASS"
        assert aggregate_review_status(results) == "ERROR"

    def test_aggregate_empty_results_is_pass(self) -> None:
        """空结果列表视为 PASS（无任何失败）。"""
        assert aggregate_review_status([]) == "PASS"

    @pytest.mark.asyncio
    async def test_json_schema_validator_error_not_pass(
        self,
        artifact_service: ArtifactServiceImpl,
        db: Database,
    ) -> None:
        """JsonSchemaValidator 返回 ERROR 时不能视为 PASS。"""
        actor = _actor(roles=["ADMIN"])
        content = _json_content({"name": "x"})
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        artifact_view = await artifact_service.get_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )

        # schema 不存在 → ERROR
        validator = JsonSchemaValidator(db, "nonexistent_schema", 1)
        result = await validator.validate(artifact_view, content)
        assert result.status == "ERROR"
        assert result.status != "PASS"


# --------------------------------------------------------------------------- #
# 验收 5：ReviewService —— submit/get/list
# --------------------------------------------------------------------------- #


class TestArtifactReviewService:
    """``ArtifactReviewServiceImpl`` CRUD 测试。"""

    @pytest.mark.asyncio
    async def test_submit_review_pass(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """提交全 PASS 的 ValidatorResult → status=PASS。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("review-pass")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        results = [
            ValidatorResult(
                status="PASS",
                issues=[],
                validator_name="hash_integrity",
                validated_at="2026-07-17T00:00:00+00:00",
            )
        ]
        view = await review_service.submit_review(
            artifact_id, results,
            actor_id=actor["user_id"], actor=actor,
        )
        assert view["artifact_id"] == artifact_id
        assert view["status"] == "PASS"
        assert view["reviewer"] == actor["user_id"]
        assert view["reviewed_at"]
        assert view["version_no"] == 1
        assert len(view["validator_results"]) == 1
        assert view["validator_results"][0]["validator_name"] == "hash_integrity"
        assert view["validator_results"][0]["status"] == "PASS"

    @pytest.mark.asyncio
    async def test_submit_review_fail(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """提交含 FAIL 的 ValidatorResult → status=FAIL。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("review-fail")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        results = [
            ValidatorResult(
                status="PASS",
                issues=[],
                validator_name="hash_integrity",
                validated_at="2026-07-17T00:00:00+00:00",
            ),
            ValidatorResult(
                status="FAIL",
                issues=[
                    ValidatorIssue(
                        severity="ERROR",
                        code="size.exceeded",
                        message="too big",
                        path=None,
                    )
                ],
                validator_name="size_limit",
                validated_at="2026-07-17T00:00:00+00:00",
            ),
        ]
        view = await review_service.submit_review(
            artifact_id, results,
            actor_id=actor["user_id"], actor=actor,
        )
        assert view["status"] == "FAIL"
        assert view["status"] != "PASS"

    @pytest.mark.asyncio
    async def test_submit_review_error_not_pass(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """提交含 ERROR 的 ValidatorResult → status=ERROR，不降级为 PASS（验收 1）。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("review-error")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        results = [
            ValidatorResult(
                status="PASS",
                issues=[],
                validator_name="hash_integrity",
                validated_at="2026-07-17T00:00:00+00:00",
            ),
            ValidatorResult(
                status="ERROR",
                issues=[
                    ValidatorIssue(
                        severity="ERROR",
                        code="json_schema.not_found",
                        message="schema missing",
                        path=None,
                    )
                ],
                validator_name="json_schema:missing:v1",
                validated_at="2026-07-17T00:00:00+00:00",
            ),
        ]
        view = await review_service.submit_review(
            artifact_id, results,
            actor_id=actor["user_id"], actor=actor,
        )
        # ERROR ≠ PASS：整体状态必须是 ERROR，不能是 PASS
        assert view["status"] == "ERROR"
        assert view["status"] != "PASS"

    @pytest.mark.asyncio
    async def test_submit_review_empty_results(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """空 ValidatorResult 列表 → status=PASS。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("review-empty")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        view = await review_service.submit_review(
            artifact_id, [],
            actor_id=actor["user_id"], actor=actor,
        )
        assert view["status"] == "PASS"
        assert view["validator_results"] == []

    @pytest.mark.asyncio
    async def test_submit_review_invalid_artifact_id(
        self, review_service: ArtifactReviewServiceImpl
    ) -> None:
        """空 artifact_id 抛 ArgumentError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(ArgumentError):
            await review_service.submit_review(
                "", [],
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_submit_review_invalid_results_type(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """validator_results 非 list 抛 ArgumentError。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("review-bad-type")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        with pytest.raises(ArgumentError):
            await review_service.submit_review(
                artifact_id, "not a list",  # type: ignore[arg-type]
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_get_review_returns_submitted(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """get_review 返回已提交的评审记录。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("review-get")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        results = [
            ValidatorResult(
                status="PASS",
                validator_name="hash_integrity",
                validated_at="2026-07-17T00:00:00+00:00",
            )
        ]
        submitted = await review_service.submit_review(
            artifact_id, results,
            actor_id=actor["user_id"], actor=actor,
        )

        fetched = await review_service.get_review(
            submitted["id"],
            actor_id=actor["user_id"], actor=actor,
        )
        assert fetched["id"] == submitted["id"]
        assert fetched["artifact_id"] == artifact_id
        assert fetched["status"] == "PASS"

    @pytest.mark.asyncio
    async def test_get_review_not_found(
        self, review_service: ArtifactReviewServiceImpl
    ) -> None:
        """get_review 不存在的 ID 抛 NotFoundError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(NotFoundError):
            await review_service.get_review(
                "nonexistent-review",
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_list_reviews_by_artifact(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """list_reviews 按 artifact_id 列出评审记录，按 reviewed_at 降序。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("review-list")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        # 提交 3 次评审
        for i in range(3):
            results = [
                ValidatorResult(
                    status="PASS",
                    validator_name=f"validator_{i}",
                    validated_at=f"2026-07-17T00:0{i}:00+00:00",
                )
            ]
            await review_service.submit_review(
                artifact_id, results,
                actor_id=actor["user_id"], actor=actor,
            )

        reviews = await review_service.list_reviews(
            artifact_id,
            actor_id=actor["user_id"], actor=actor,
        )
        assert len(reviews) == 3
        # 全部关联到同一个 artifact_id
        assert all(r["artifact_id"] == artifact_id for r in reviews)
        # reviewed_at 降序（最新在前）
        timestamps = [r["reviewed_at"] for r in reviews]
        assert timestamps == sorted(timestamps, reverse=True)

    @pytest.mark.asyncio
    async def test_list_reviews_empty(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """无评审记录的 artifact 返回空列表。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("review-empty-list")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        reviews = await review_service.list_reviews(
            artifact_id,
            actor_id=actor["user_id"], actor=actor,
        )
        assert reviews == []

    @pytest.mark.asyncio
    async def test_list_reviews_invalid_artifact_id(
        self, review_service: ArtifactReviewServiceImpl
    ) -> None:
        """空 artifact_id 抛 ArgumentError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(ArgumentError):
            await review_service.list_reviews(
                "",
                actor_id=actor["user_id"], actor=actor,
            )


# --------------------------------------------------------------------------- #
# 验收 6：权限检查
# --------------------------------------------------------------------------- #


class TestReviewPermissions:
    """``ArtifactReviewServiceImpl`` 权限检查测试。"""

    @pytest.mark.asyncio
    async def test_observer_can_read_reviews_but_not_write(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """OBSERVER 可读但不可写 reviews（DEFAULT_POLICIES 含 OBSERVER read *）。"""
        admin = _actor(roles=["ADMIN"])
        observer = _actor(user_id="obs", roles=["OBSERVER"])
        content = _content("perm-read-write")
        artifact_id = await _upload(
            artifact_service, content=content, actor=admin
        )

        # admin 提交评审
        results = [
            ValidatorResult(
                status="PASS",
                validator_name="hash_integrity",
                validated_at="2026-07-17T00:00:00+00:00",
            )
        ]
        submitted = await review_service.submit_review(
            artifact_id, results,
            actor_id=admin["user_id"], actor=admin,
        )

        # observer 可读
        fetched = await review_service.get_review(
            submitted["id"],
            actor_id=observer["user_id"], actor=observer,
        )
        assert fetched["id"] == submitted["id"]

        listed = await review_service.list_reviews(
            artifact_id,
            actor_id=observer["user_id"], actor=observer,
        )
        assert len(listed) == 1

        # observer 不可写（submit_review 需 write reviews）
        with pytest.raises(PermissionDeniedError):
            await review_service.submit_review(
                artifact_id, results,
                actor_id=observer["user_id"], actor=observer,
            )

    @pytest.mark.asyncio
    async def test_approver_can_read_and_write_reviews(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """APPROVER 可读可写 reviews（DEFAULT_POLICIES 含 APPROVER reviews .*）。"""
        admin = _actor(roles=["ADMIN"])
        approver = _actor(user_id="approver", roles=["APPROVER"])
        content = _content("perm-approver")
        artifact_id = await _upload(
            artifact_service, content=content, actor=admin
        )

        results = [
            ValidatorResult(
                status="PASS",
                validator_name="hash_integrity",
                validated_at="2026-07-17T00:00:00+00:00",
            )
        ]
        # APPROVER 可写
        view = await review_service.submit_review(
            artifact_id, results,
            actor_id=approver["user_id"], actor=approver,
        )
        # APPROVER 可读
        fetched = await review_service.get_review(
            view["id"],
            actor_id=approver["user_id"], actor=approver,
        )
        assert fetched["id"] == view["id"]

    @pytest.mark.asyncio
    async def test_no_roles_denied(
        self, review_service: ArtifactReviewServiceImpl
    ) -> None:
        """无角色用户被拒绝所有操作。"""
        nobody = _actor(user_id="nobody", roles=[])
        with pytest.raises(PermissionDeniedError):
            await review_service.get_review(
                "any", actor_id=nobody["user_id"], actor=nobody,
            )
        with pytest.raises(PermissionDeniedError):
            await review_service.list_reviews(
                "any", actor_id=nobody["user_id"], actor=nobody,
            )

    @pytest.mark.asyncio
    async def test_owner_can_read_and_write_reviews(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """OWNER 可读可写 reviews（本测试 fixture 追加 OWNER reviews 策略）。"""
        admin = _actor(roles=["ADMIN"])
        owner = _actor(user_id="owner", roles=["OWNER"])
        content = _content("perm-owner")
        artifact_id = await _upload(
            artifact_service, content=content, actor=admin
        )

        results = [
            ValidatorResult(
                status="PASS",
                validator_name="hash_integrity",
                validated_at="2026-07-17T00:00:00+00:00",
            )
        ]
        view = await review_service.submit_review(
            artifact_id, results,
            actor_id=owner["user_id"], actor=owner,
        )
        assert view["status"] == "PASS"

        fetched = await review_service.get_review(
            view["id"],
            actor_id=owner["user_id"], actor=owner,
        )
        assert fetched["id"] == view["id"]


# --------------------------------------------------------------------------- #
# 验收 7：artifact_reviews 表 DDL
# --------------------------------------------------------------------------- #


class TestArtifactReviewsDdl:
    """``artifact_reviews`` 表 DDL 约束测试。"""

    @pytest.mark.asyncio
    async def test_invalid_json_rejected_by_check(
        self, db: Database
    ) -> None:
        """``CHECK(json_valid(validator_results))`` 拒绝非 JSON 文本。"""
        async with db.write_connection() as conn:
            with pytest.raises(Exception):  # noqa: BLE001 —— aiosqlite.IntegrityError
                await conn.execute(
                    "INSERT INTO artifact_reviews "
                    "(id, artifact_id, status, validator_results, reviewer, "
                    "reviewed_at, version_no) VALUES (?, ?, ?, ?, ?, ?, 1)",
                    (
                        "rev-1",
                        "art-1",
                        "PASS",
                        "not-a-json-string",
                        "u",
                        "2026-01-01",
                    ),
                )

    @pytest.mark.asyncio
    async def test_status_values_stored(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        db: Database,
    ) -> None:
        """PASS/FAIL/ERROR 三种 status 都能正确存储与读取。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("ddl-status")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        for expected_status, validator_status in [
            ("PASS", "PASS"),
            ("FAIL", "FAIL"),
            ("ERROR", "ERROR"),
        ]:
            results = [
                ValidatorResult(
                    status=validator_status,  # type: ignore[arg-type]
                    validator_name="test",
                    validated_at="2026-07-17T00:00:00+00:00",
                )
            ]
            view = await review_service.submit_review(
                artifact_id, results,
                actor_id=actor["user_id"], actor=actor,
            )
            assert view["status"] == expected_status

        # 从 DB 直接读取验证
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT status FROM artifact_reviews "
                "WHERE artifact_id = ? ORDER BY reviewed_at ASC",
                (artifact_id,),
            ) as cur:
                rows = await cur.fetchall()
        statuses = [r[0] for r in rows]
        assert set(statuses) == {"PASS", "FAIL", "ERROR"}

    @pytest.mark.asyncio
    async def test_validator_results_json_roundtrip(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        db: Database,
    ) -> None:
        """validator_results JSON 序列化/反序列化保持完整。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("ddl-json")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        results = [
            ValidatorResult(
                status="FAIL",
                issues=[
                    ValidatorIssue(
                        severity="ERROR",
                        code="test.code",
                        message="test message with unicode: 中文",
                        path="$.nested.field",
                    ),
                    ValidatorIssue(
                        severity="WARNING",
                        code="test.warn",
                        message="warning",
                        path=None,
                    ),
                ],
                validator_name="complex_validator",
                validated_at="2026-07-17T12:34:56+00:00",
            )
        ]
        view = await review_service.submit_review(
            artifact_id, results,
            actor_id=actor["user_id"], actor=actor,
        )

        # 重新读取
        fetched = await review_service.get_review(
            view["id"],
            actor_id=actor["user_id"], actor=actor,
        )
        assert len(fetched["validator_results"]) == 1
        vr = fetched["validator_results"][0]
        assert vr["status"] == "FAIL"
        assert vr["validator_name"] == "complex_validator"
        assert vr["validated_at"] == "2026-07-17T12:34:56+00:00"
        assert len(vr["issues"]) == 2
        # unicode message 保持完整
        messages = [i["message"] for i in vr["issues"]]
        assert any("中文" in m for m in messages)
        # path 保持完整
        paths = [i["path"] for i in vr["issues"]]
        assert "$.nested.field" in paths
        assert None in paths


# --------------------------------------------------------------------------- #
# 验收 8：端到端 —— Registry + ReviewService 集成
# --------------------------------------------------------------------------- #


class TestEndToEndIntegration:
    """端到端：上传 artifact → 注册 schema → Registry 校验 → 提交评审。"""

    @pytest.mark.asyncio
    async def test_full_flow_pass(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
        registry: ValidatorRegistry,
        review_service: ArtifactReviewServiceImpl,
        db: Database,
    ) -> None:
        """完整流程：合规 artifact → 全 PASS → 评审 PASS。"""
        actor = _actor(roles=["ADMIN"])
        await _register_schema(schema_service, actor=actor)
        content = _json_content({"name": "e2e-pass", "version": 1})
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        registry.register(HashIntegrityValidator())
        registry.register(SizeLimitValidator(max_size_bytes=1024 * 1024))
        registry.register(JsonSchemaValidator(db, "task_payload", 1))

        results = await registry.validate_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )
        # 3 个 Validator 都运行了
        assert len(results) == 3
        assert all(r.status == "PASS" for r in results)
        assert aggregate_review_status(results) == "PASS"

        view = await review_service.submit_review(
            artifact_id, results,
            actor_id=actor["user_id"], actor=actor,
        )
        assert view["status"] == "PASS"

    @pytest.mark.asyncio
    async def test_full_flow_fail(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
        registry: ValidatorRegistry,
        review_service: ArtifactReviewServiceImpl,
        db: Database,
    ) -> None:
        """完整流程：违规 artifact → FAIL → 评审 FAIL。"""
        actor = _actor(roles=["ADMIN"])
        await _register_schema(schema_service, actor=actor)
        # 缺 name 字段
        content = _json_content({"version": 1})
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        registry.register(HashIntegrityValidator())
        registry.register(JsonSchemaValidator(db, "task_payload", 1))

        results = await registry.validate_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )
        # hash PASS, schema FAIL
        statuses = {r.validator_name: r.status for r in results}
        assert statuses.get("hash_integrity") == "PASS"
        assert statuses.get("json_schema:task_payload:v1") == "FAIL"
        assert aggregate_review_status(results) == "FAIL"

        view = await review_service.submit_review(
            artifact_id, results,
            actor_id=actor["user_id"], actor=actor,
        )
        assert view["status"] == "FAIL"
        assert view["status"] != "PASS"

    @pytest.mark.asyncio
    async def test_full_flow_error_not_pass(
        self,
        artifact_service: ArtifactServiceImpl,
        registry: ValidatorRegistry,
        review_service: ArtifactReviewServiceImpl,
        db: Database,
    ) -> None:
        """完整流程：Validator ERROR → 评审 ERROR（不降级为 PASS）。"""
        actor = _actor(roles=["ADMIN"])
        content = _json_content({"name": "e2e-error"})
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        registry.register(HashIntegrityValidator())
        # schema 不存在 → ERROR
        registry.register(JsonSchemaValidator(db, "nonexistent_schema", 1))

        results = await registry.validate_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )
        statuses = {r.validator_name: r.status for r in results}
        assert statuses.get("hash_integrity") == "PASS"
        assert statuses.get("json_schema:nonexistent_schema:v1") == "ERROR"
        # 整体 ERROR
        assert aggregate_review_status(results) == "ERROR"

        view = await review_service.submit_review(
            artifact_id, results,
            actor_id=actor["user_id"], actor=actor,
        )
        # ERROR ≠ PASS
        assert view["status"] == "ERROR"
        assert view["status"] != "PASS"

    @pytest.mark.asyncio
    async def test_registry_then_list_reviews(
        self,
        artifact_service: ArtifactServiceImpl,
        registry: ValidatorRegistry,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """校验后提交评审，list_reviews 能查到。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("e2e-list")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        registry.register(HashIntegrityValidator())
        results = await registry.validate_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )

        await review_service.submit_review(
            artifact_id, results,
            actor_id=actor["user_id"], actor=actor,
        )

        reviews = await review_service.list_reviews(
            artifact_id,
            actor_id=actor["user_id"], actor=actor,
        )
        assert len(reviews) == 1
        assert reviews[0]["artifact_id"] == artifact_id
        assert reviews[0]["status"] == "PASS"


# --------------------------------------------------------------------------- #
# 验收 9：事件经 Outbox 写入
# --------------------------------------------------------------------------- #


class TestReviewEvents:
    """``submit_review`` 后 ``artifact.review_submitted`` 事件写入 Outbox。"""

    @pytest.mark.asyncio
    async def test_submit_review_writes_event(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        db: Database,
    ) -> None:
        """submit_review 后 outbox_events 含 artifact.review_submitted 事件。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("event-test")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        results = [
            ValidatorResult(
                status="PASS",
                validator_name="hash_integrity",
                validated_at="2026-07-17T00:00:00+00:00",
            )
        ]
        view = await review_service.submit_review(
            artifact_id, results,
            actor_id=actor["user_id"], actor=actor,
        )

        # 直接从 outbox_events 表查询
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT id, event_type, aggregate_type, aggregate_id, "
                "payload FROM outbox_events "
                "WHERE event_type = ? ORDER BY occurred_at ASC",
                ("artifact.review_submitted",),
            ) as cur:
                rows = await cur.fetchall()

        assert len(rows) == 1
        evt = rows[0]
        assert evt[1] == "artifact.review_submitted"  # event_type
        assert evt[2] == "artifact_review"  # aggregate_type
        assert evt[3] == view["id"]  # aggregate_id
        payload = json.loads(evt[4])
        assert payload["artifact_id"] == artifact_id
        assert payload["status"] == "PASS"
        assert payload["validator_count"] == 1
        assert payload["validator_names"] == ["hash_integrity"]


# --------------------------------------------------------------------------- #
# 验收 10：不破坏 TASK-078/079
# --------------------------------------------------------------------------- #


class TestBackwardCompatibility:
    """确保 TASK-080 不破坏 TASK-078 的 ArtifactService 与 TASK-079 的
    ArtifactSchemaService。"""

    @pytest.mark.asyncio
    async def test_artifact_service_still_works(
        self, artifact_service: ArtifactServiceImpl
    ) -> None:
        """TASK-078 的 upload/get/download 仍正常工作。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("backward-compat-artifact")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        # get
        view = await artifact_service.get_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )
        assert view["id"] == artifact_id

        # download
        downloaded = await artifact_service.download_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )
        assert downloaded == content

    @pytest.mark.asyncio
    async def test_schema_service_still_works(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """TASK-079 的 register_schema/validate_artifact 仍正常工作。"""
        actor = _actor(roles=["DESIGNER"])
        await _register_schema(schema_service, actor=actor)
        content = _json_content({"name": "backward-compat-schema"})
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        # validate_artifact（TASK-079 的方法）
        result = await schema_service.validate_artifact(
            artifact_id, "task_payload", 1,
            actor_id=actor["user_id"], actor=actor,
        )
        assert result["valid"] is True
        assert result["issues"] == []

    @pytest.mark.asyncio
    async def test_existing_validation_result_typeddict_preserved(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """TASK-079 的 ``ValidationResult`` TypedDict（含 valid/issues 字段）保留。

        TASK-080 新增的 ``ValidatorResult`` dataclass 是不同类型（含 status 字段），
        两者不冲突。
        """
        from maf_server.modules.artifacts.schemas import (
            ValidationIssue as SchemaValidationIssue,
        )
        from maf_server.modules.artifacts.schemas import (
            ValidationResult as SchemaValidationResult,
        )
        from maf_server.modules.artifacts.service import (
            ValidatorIssue as NewValidatorIssue,
            ValidatorResult as NewValidatorResult,
        )

        # TASK-079 的 ValidationResult 是 TypedDict，含 valid 字段
        old_result: SchemaValidationResult = {
            "artifact_id": "x",
            "content_hash": "y",
            "schema_name": "s",
            "schema_version": 1,
            "valid": True,
            "issues": [],
        }
        assert old_result["valid"] is True

        # TASK-080 的 ValidatorResult 是 dataclass，含 status 字段
        new_result = NewValidatorResult(
            status="PASS",
            issues=[],
            validator_name="test",
            validated_at="2026-07-17T00:00:00+00:00",
        )
        assert new_result.status == "PASS"

        # 两种类型不冲突
        assert SchemaValidationResult is not NewValidatorResult
        assert SchemaValidationIssue is not NewValidatorIssue
