"""TASK-079 集成测试：ArtifactSchema 血缘与 Diff。

验收标准覆盖（对应 TASK-079 文档）：

1. **ArtifactSchemaService**：``register_schema``/``get_schema``/``list_schemas``/
   ``validate_artifact``/``deprecate_schema``。
2. **血缘追踪（lineage）**：``record_lineage``/``get_lineage``/``get_upstream``/
   ``get_downstream``，禁止成环与跨项目泄露。
3. **Diff**：``diff_artifacts`` 返回结构化 added/removed/modified。
4. **数据库表**：``artifact_schemas``（含 ``json_valid`` CHECK）、
   ``artifact_lineage``（复合 PK）。
5. **内容 hash 不可变**：所有 schema/lineage/diff 操作不修改已存储 artifact 的
   ``content_hash``。
6. **权限**：通过 ``PermissionService.require`` 检查。
7. **事件**：``artifact.schema_registered``、``artifact.schema_deprecated``、
   ``artifact.lineage_recorded`` 经 ``OutboxRepository`` 写入。

不破坏 TASK-078 的 ``ArtifactService`` 与 TASK-011 的 ``SchemaLoader``；
不修改已存储 artifact 的 ``content_hash``。
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
    AlreadyExistsError,
    ArgumentError,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedOperationError,
    VersionConflictError,
)
from maf_policy import CasbinPermissionService
from maf_server.config import ServerSettings
from maf_server.core.artifact_store import LocalArtifactFileStore
from maf_server.core.database import Database
from maf_server.core.events import (
    SqliteOutboxRepository,
    init_outbox_schema,
)
from maf_server.core.unit_of_work import SqliteUnitOfWork
from maf_server.modules.artifacts.repository import (
    SqliteArtifactRepository,
    init_schema as init_artifact_schema,
)
from maf_server.modules.artifacts.service import (
    ArtifactSchemaServiceImpl,
    ArtifactServiceImpl,
)

# packages/artifact_schemas/src 需要在 sys.path 中（pyproject.toml pythonpath 未含），
# 与 tests/integration/test_control_init.py 一致。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT_SCHEMAS_SRC = _PROJECT_ROOT / "packages" / "artifact_schemas" / "src"
if str(_ARTIFACT_SCHEMAS_SRC) not in sys.path:
    sys.path.insert(0, str(_ARTIFACT_SCHEMAS_SRC))

_SECRET_PLAINTEXT = "test-secret-for-artifact-task-079"


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
    """构造带 artifact 与 artifact_schemas 策略的 CasbinPermissionService。

    DEFAULT_POLICIES 不含 artifacts / artifact_schemas 资源；本测试在 service
    实例上追加 OWNER/DESIGNER 的 artifacts 与 artifact_schemas 读写策略。
    ADMIN 默认拥有 ``*`` ``.*`` 全权；OBSERVER 通过 ``*`` ``read`` 只读。
    """
    service = CasbinPermissionService()
    service.add_policy("OWNER", "artifacts", "(read|write)")
    service.add_policy("DESIGNER", "artifacts", "(read|write)")
    service.add_policy("APPROVER", "artifacts", "read")
    service.add_policy("OWNER", "artifact_schemas", "(read|write)")
    service.add_policy("DESIGNER", "artifact_schemas", "(read|write)")
    service.add_policy("APPROVER", "artifact_schemas", "read")
    return service


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    """已初始化并建好 artifacts / artifact_schemas / artifact_lineage / outbox 表。"""
    settings = _make_settings(tmp_path)
    database = Database(settings)
    await database.initialize()
    # 建 artifacts + artifact_schemas + artifact_lineage 表
    async with database.write_connection() as conn:
        await init_artifact_schema(conn)
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


def _actor(
    user_id: str = "user-admin",
    roles: list[str] | None = None,
    trace_id: str = "schema-trace",
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


def _content(data: str = "hello schema world") -> bytes:
    """测试用内容。"""
    return data.encode("utf-8")


def _json_content(obj: Any) -> bytes:
    """把 Python 对象序列化为 JSON bytes。"""
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _simple_schema() -> dict[str, Any]:
    """构造一个简单的 JSON Schema（要求 ``name`` 必填，``version`` 为正整数）。"""
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
        content = _content("default-lineage-content")
    view = await service.upload_artifact(
        project_id,
        artifact_type,
        content,
        actor_id=actor["user_id"],
        actor=actor,
    )
    return view["id"]


# --------------------------------------------------------------------------- #
# 验收 1：ArtifactSchemaService 注册/获取/列表/废弃
# --------------------------------------------------------------------------- #


class TestSchemaRegistration:
    """``register_schema`` / ``get_schema`` / ``list_schemas`` /
    ``deprecate_schema`` 集成测试。"""

    @pytest.mark.asyncio
    async def test_register_schema_creates_active_version(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """注册新 Schema 创建 ACTIVE 状态版本，version_no=1。"""
        actor = _actor(roles=["DESIGNER"], user_id="designer-1")
        view = await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )

        assert view["schema_name"] == "task_payload"
        assert view["version"] == 1
        assert view["status"] == "ACTIVE"
        assert view["version_no"] == 1
        assert view["created_by"] == "designer-1"
        assert view["created_at"]  # ISO 字符串非空
        # json_schema 完整保留
        assert view["json_schema"]["type"] == "object"
        assert "name" in view["json_schema"]["required"]

    @pytest.mark.asyncio
    async def test_register_schema_duplicate_raises(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """重复注册 (schema_name, version) 抛 AlreadyExistsError。"""
        actor = _actor(roles=["DESIGNER"])
        await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )

        with pytest.raises(AlreadyExistsError):
            await schema_service.register_schema(
                "task_payload", 1, _simple_schema(),
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_register_schema_invalid_name_rejected(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """非法 schema_name（含大写、特殊字符）抛 ArgumentError。"""
        actor = _actor(roles=["DESIGNER"])
        invalid_names = ["Task_Payload", "task-payload", "1task", "task.payload", ""]
        for bad_name in invalid_names:
            with pytest.raises(ArgumentError):
                await schema_service.register_schema(
                    bad_name, 1, _simple_schema(),
                    actor_id=actor["user_id"], actor=actor,
                )

    @pytest.mark.asyncio
    async def test_register_schema_invalid_version_rejected(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """version < 1 抛 ArgumentError。"""
        actor = _actor(roles=["DESIGNER"])
        with pytest.raises(ArgumentError):
            await schema_service.register_schema(
                "task_payload", 0, _simple_schema(),
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_register_schema_invalid_json_schema_rejected(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """非法 JSON Schema（type 不是合法类型）抛 ValidationError。"""
        from maf_domain.errors import ValidationError

        actor = _actor(roles=["DESIGNER"])
        bad_schema = {"type": "not-a-real-type"}
        with pytest.raises(ValidationError):
            await schema_service.register_schema(
                "task_payload", 1, bad_schema,
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_get_schema_returns_registered(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """get_schema 返回已注册的 Schema。"""
        actor = _actor(roles=["DESIGNER"])
        await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )

        view = await schema_service.get_schema(
            "task_payload", 1,
            actor_id=actor["user_id"], actor=actor,
        )
        assert view["schema_name"] == "task_payload"
        assert view["version"] == 1

    @pytest.mark.asyncio
    async def test_get_schema_not_found(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """get_schema 不存在的 Schema 抛 NotFoundError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(NotFoundError):
            await schema_service.get_schema(
                "nonexistent", 1,
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_list_schemas_all(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """list_schemas 返回所有 Schema 版本，按 (name, version) 升序。"""
        actor = _actor(roles=["DESIGNER"])
        await schema_service.register_schema(
            "alpha", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )
        await schema_service.register_schema(
            "alpha", 2, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )
        await schema_service.register_schema(
            "beta", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )

        views = await schema_service.list_schemas(
            actor_id=actor["user_id"], actor=actor,
        )
        # 按 (schema_name, version) 升序：alpha v1, alpha v2, beta v1
        assert [(v["schema_name"], v["version"]) for v in views] == [
            ("alpha", 1), ("alpha", 2), ("beta", 1),
        ]

    @pytest.mark.asyncio
    async def test_list_schemas_filter_by_name(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """list_schemas 按 schema_name 过滤。"""
        actor = _actor(roles=["DESIGNER"])
        await schema_service.register_schema(
            "alpha", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )
        await schema_service.register_schema(
            "beta", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )

        views = await schema_service.list_schemas(
            schema_name="alpha",
            actor_id=actor["user_id"], actor=actor,
        )
        assert len(views) == 1
        assert views[0]["schema_name"] == "alpha"

    @pytest.mark.asyncio
    async def test_deprecate_schema_marks_deprecated(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """deprecate_schema 把 ACTIVE → DEPRECATED，version_no 递增。"""
        actor = _actor(roles=["DESIGNER"])
        await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )

        view = await schema_service.deprecate_schema(
            "task_payload", 1,
            actor_id=actor["user_id"], actor=actor,
        )
        assert view["status"] == "DEPRECATED"
        assert view["version_no"] == 2

    @pytest.mark.asyncio
    async def test_deprecate_schema_already_deprecated_raises(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """deprecate_schema 对已废弃的 Schema 抛 UnsupportedOperationError。"""
        actor = _actor(roles=["DESIGNER"])
        await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )
        await schema_service.deprecate_schema(
            "task_payload", 1,
            actor_id=actor["user_id"], actor=actor,
        )

        with pytest.raises(UnsupportedOperationError):
            await schema_service.deprecate_schema(
                "task_payload", 1,
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_deprecate_schema_not_found(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """deprecate_schema 不存在的 Schema 抛 NotFoundError。"""
        actor = _actor(roles=["DESIGNER"])
        with pytest.raises(NotFoundError):
            await schema_service.deprecate_schema(
                "nonexistent", 1,
                actor_id=actor["user_id"], actor=actor,
            )


# --------------------------------------------------------------------------- #
# 验收 1（续）：乐观锁
# --------------------------------------------------------------------------- #


class TestSchemaOptimisticLock:
    """``deprecate_schema`` 乐观锁测试。"""

    @pytest.mark.asyncio
    async def test_deprecate_version_conflict(
        self,
        schema_service: ArtifactSchemaServiceImpl,
        db: Database,
    ) -> None:
        """deprecate_schema 乐观锁冲突抛 VersionConflictError。

        使用 stale-version repository 包装器：service 读到 version_no=0，
        DB 实际 version_no=1，UPDATE 不匹配，抛 VersionConflictError。
        """
        from dataclasses import replace

        from maf_server.modules.artifacts.repository import (
            ArtifactSchemaRecord,
            SqliteArtifactSchemaRepository,
        )

        actor = _actor(roles=["DESIGNER"])
        await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )

        class _StaleSchemaRepository:
            """包装 schema repository，``get_schema`` 返回过期 version_no。"""

            def __init__(self, inner: SqliteArtifactSchemaRepository) -> None:
                self._inner = inner

            async def get_schema(self, conn, name, version):
                rec = await self._inner.get_schema(conn, name, version)
                if rec is not None:
                    return replace(rec, version_no=max(0, rec.version_no - 1))
                return rec

            def __getattr__(self, name):
                return getattr(self._inner, name)

        stale_service = ArtifactSchemaServiceImpl(
            database=db,
            file_store=schema_service._file_store,
            schema_repository=_StaleSchemaRepository(SqliteArtifactSchemaRepository()),
            permission_service=_make_permission_service(),
        )

        with pytest.raises(VersionConflictError):
            await stale_service.deprecate_schema(
                "task_payload", 1,
                actor_id=actor["user_id"], actor=actor,
            )


# --------------------------------------------------------------------------- #
# 验收 2：validate_artifact
# --------------------------------------------------------------------------- #


class TestValidateArtifact:
    """``validate_artifact`` 集成测试。"""

    @pytest.mark.asyncio
    async def test_validate_valid_artifact(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """合法 artifact 通过校验，valid=True，issues 为空。"""
        actor = _actor(roles=["DESIGNER"])
        await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )
        # 上传符合 Schema 的 artifact 内容
        artifact_id = await _upload(
            artifact_service,
            content=_json_content({"name": "task-1", "version": 1}),
            actor=actor,
        )

        result = await schema_service.validate_artifact(
            artifact_id, "task_payload", 1,
            actor_id=actor["user_id"], actor=actor,
        )
        assert result["valid"] is True
        assert result["issues"] == []
        assert result["schema_name"] == "task_payload"
        assert result["schema_version"] == 1
        assert result["artifact_id"] == artifact_id

    @pytest.mark.asyncio
    async def test_validate_invalid_artifact_missing_required(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """缺失 required 字段 → valid=False，issues 含字段路径。"""
        actor = _actor(roles=["DESIGNER"])
        await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )
        # 缺少 name 字段
        artifact_id = await _upload(
            artifact_service,
            content=_json_content({"version": 1}),
            actor=actor,
        )

        result = await schema_service.validate_artifact(
            artifact_id, "task_payload", 1,
            actor_id=actor["user_id"], actor=actor,
        )
        assert result["valid"] is False
        assert len(result["issues"]) >= 1
        # required 错误路径应解析为 $.name（而不是 $）
        field_paths = [i["field_path"] for i in result["issues"]]
        assert any("name" in p for p in field_paths)

    @pytest.mark.asyncio
    async def test_validate_invalid_artifact_additional_property(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """additionalProperties=False 时多字段 → valid=False。"""
        actor = _actor(roles=["DESIGNER"])
        await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )
        # 多了 extra 字段
        artifact_id = await _upload(
            artifact_service,
            content=_json_content({"name": "x", "extra": "bad"}),
            actor=actor,
        )

        result = await schema_service.validate_artifact(
            artifact_id, "task_payload", 1,
            actor_id=actor["user_id"], actor=actor,
        )
        assert result["valid"] is False
        field_paths = [i["field_path"] for i in result["issues"]]
        assert any("extra" in p for p in field_paths)

    @pytest.mark.asyncio
    async def test_validate_non_json_artifact(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """非 JSON 内容 → valid=False，issues 含 '$' 路径。"""
        actor = _actor(roles=["DESIGNER"])
        await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )
        artifact_id = await _upload(
            artifact_service,
            content=b"not a json document at all",
            actor=actor,
        )

        result = await schema_service.validate_artifact(
            artifact_id, "task_payload", 1,
            actor_id=actor["user_id"], actor=actor,
        )
        assert result["valid"] is False
        assert len(result["issues"]) == 1
        assert result["issues"][0]["field_path"] == "$"

    @pytest.mark.asyncio
    async def test_validate_artifact_not_found(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """不存在的 artifact 抛 NotFoundError。"""
        actor = _actor(roles=["DESIGNER"])
        await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )

        with pytest.raises(NotFoundError):
            await schema_service.validate_artifact(
                "nonexistent-id", "task_payload", 1,
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_validate_schema_not_found(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """不存在的 Schema 抛 NotFoundError。"""
        actor = _actor(roles=["DESIGNER"])
        artifact_id = await _upload(artifact_service, actor=actor)

        with pytest.raises(NotFoundError):
            await schema_service.validate_artifact(
                artifact_id, "nonexistent", 1,
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_validate_artifact_hash_immutable(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
        db: Database,
    ) -> None:
        """validate_artifact 不修改 artifact 的 content_hash（验收：内容 hash 不可变）。"""
        actor = _actor(roles=["DESIGNER"])
        await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )
        content = _json_content({"name": "task-immutable"})
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor,
        )

        # 记录原始 content_hash
        original_hash = hashlib.sha256(content).hexdigest()

        # 多次校验
        for _ in range(3):
            await schema_service.validate_artifact(
                artifact_id, "task_payload", 1,
                actor_id=actor["user_id"], actor=actor,
            )

        # 直接从 DB 读取 content_hash，验证未被修改
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT content_hash FROM artifacts WHERE id = ?",
                (artifact_id,),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        assert row[0] == original_hash


# --------------------------------------------------------------------------- #
# 验收 2（续）：DDL —— json_valid CHECK
# --------------------------------------------------------------------------- #


class TestSchemaDdl:
    """``artifact_schemas`` 与 ``artifact_lineage`` 表 DDL 约束测试。"""

    @pytest.mark.asyncio
    async def test_invalid_json_rejected_by_check(
        self, db: Database
    ) -> None:
        """``CHECK(json_valid(json_schema))`` 拒绝非 JSON 文本。"""
        async with db.write_connection() as conn:
            with pytest.raises(Exception):  # noqa: BLE001 —— aiosqlite.IntegrityError
                await conn.execute(
                    "INSERT INTO artifact_schemas "
                    "(schema_name, version, json_schema, status, created_by, "
                    "created_at, version_no) VALUES (?, ?, ?, ?, ?, ?, 1)",
                    ("bad", 1, "not-a-json-string", "ACTIVE", "u", "2026-01-01"),
                )

    @pytest.mark.asyncio
    async def test_lineage_composite_pk_dedup(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
        db: Database,
    ) -> None:
        """artifact_lineage 复合 PK (artifact_id, parent_artifact_id, relation) 去重。"""
        actor = _actor(roles=["ADMIN"])
        parent_id = await _upload(
            artifact_service, content=_content("parent"), actor=actor,
        )
        child_id = await _upload(
            artifact_service, content=_content("child"), actor=actor,
        )

        # 第一次记录
        await schema_service.record_lineage(
            child_id,
            parent_artifact_ids=[parent_id],
            actor_id=actor["user_id"], actor=actor,
        )
        # 第二次相同 (child, parent, relation) —— service 层幂等返回，不抛错
        edges = await schema_service.record_lineage(
            child_id,
            parent_artifact_ids=[parent_id],
            actor_id=actor["user_id"], actor=actor,
        )
        assert len(edges) == 1
        assert edges[0]["artifact_id"] == child_id
        assert edges[0]["parent_artifact_id"] == parent_id

        # 直接从 DB 验证只有一条边
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM artifact_lineage "
                "WHERE artifact_id = ? AND parent_artifact_id = ?",
                (child_id, parent_id),
            ) as cur:
                row = await cur.fetchone()
        assert row[0] == 1


# --------------------------------------------------------------------------- #
# 验收 3：血缘 record_lineage / get_lineage / get_upstream / get_downstream
# --------------------------------------------------------------------------- #


class TestArtifactLineage:
    """血缘追踪测试。"""

    @pytest.mark.asyncio
    async def test_record_lineage_creates_edge(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """record_lineage 创建血缘边，返回 LineageEdge 列表。"""
        actor = _actor(roles=["ADMIN"])
        parent_id = await _upload(
            artifact_service, content=_content("parent-1"), actor=actor,
        )
        child_id = await _upload(
            artifact_service, content=_content("child-1"), actor=actor,
        )

        edges = await schema_service.record_lineage(
            child_id,
            parent_artifact_ids=[parent_id],
            transformation="pytest -> junit xml",
            relation="DERIVED_FROM",
            actor_id=actor["user_id"], actor=actor,
        )

        assert len(edges) == 1
        edge = edges[0]
        assert edge["artifact_id"] == child_id
        assert edge["parent_artifact_id"] == parent_id
        assert edge["relation"] == "DERIVED_FROM"
        assert edge["transformation"] == "pytest -> junit xml"
        assert edge["recorded_by"] == actor["user_id"]
        assert edge["recorded_at"]  # ISO 非空

    @pytest.mark.asyncio
    async def test_record_lineage_multiple_parents(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """record_lineage 一次记录多个 parent。"""
        actor = _actor(roles=["ADMIN"])
        p1 = await _upload(artifact_service, content=_content("p1"), actor=actor)
        p2 = await _upload(artifact_service, content=_content("p2"), actor=actor)
        child = await _upload(artifact_service, content=_content("c"), actor=actor)

        edges = await schema_service.record_lineage(
            child,
            parent_artifact_ids=[p1, p2],
            relation="IMPLEMENTS",
            actor_id=actor["user_id"], actor=actor,
        )
        assert len(edges) == 2
        parent_ids = {e["parent_artifact_id"] for e in edges}
        assert parent_ids == {p1, p2}
        # 所有 edge 关系一致
        assert all(e["relation"] == "IMPLEMENTS" for e in edges)

    @pytest.mark.asyncio
    async def test_record_lineage_self_loop_rejected(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """parent_artifact_ids 含自身抛 ArgumentError。"""
        actor = _actor(roles=["ADMIN"])
        a_id = await _upload(artifact_service, actor=actor)

        with pytest.raises(ArgumentError):
            await schema_service.record_lineage(
                a_id,
                parent_artifact_ids=[a_id],
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_record_lineage_empty_parents_rejected(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """parent_artifact_ids 为空抛 ArgumentError。"""
        actor = _actor(roles=["ADMIN"])
        a_id = await _upload(artifact_service, actor=actor)

        with pytest.raises(ArgumentError):
            await schema_service.record_lineage(
                a_id,
                parent_artifact_ids=[],
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_record_lineage_invalid_relation_rejected(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """未知 relation 抛 ArgumentError。"""
        actor = _actor(roles=["ADMIN"])
        parent = await _upload(artifact_service, actor=actor)
        child = await _upload(artifact_service, actor=actor)

        with pytest.raises(ArgumentError):
            await schema_service.record_lineage(
                child,
                parent_artifact_ids=[parent],
                relation="UNKNOWN_RELATION",
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_record_lineage_artifact_not_found(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """artifact 不存在抛 NotFoundError。"""
        actor = _actor(roles=["ADMIN"])
        parent = await _upload(artifact_service, actor=actor)

        with pytest.raises(NotFoundError):
            await schema_service.record_lineage(
                "nonexistent-child",
                parent_artifact_ids=[parent],
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_record_lineage_parent_not_found(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """parent 不存在抛 NotFoundError。"""
        actor = _actor(roles=["ADMIN"])
        child = await _upload(artifact_service, actor=actor)

        with pytest.raises(NotFoundError):
            await schema_service.record_lineage(
                child,
                parent_artifact_ids=["nonexistent-parent"],
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_get_upstream_returns_direct_parents(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """get_upstream 返回直接上游 artifact 列表。"""
        actor = _actor(roles=["ADMIN"])
        p1 = await _upload(artifact_service, content=_content("p1"), actor=actor)
        p2 = await _upload(artifact_service, content=_content("p2"), actor=actor)
        child = await _upload(artifact_service, content=_content("c"), actor=actor)

        await schema_service.record_lineage(
            child, parent_artifact_ids=[p1, p2],
            actor_id=actor["user_id"], actor=actor,
        )

        upstream = await schema_service.get_upstream(
            child, actor_id=actor["user_id"], actor=actor,
        )
        upstream_ids = {u["id"] for u in upstream}
        assert upstream_ids == {p1, p2}

    @pytest.mark.asyncio
    async def test_get_downstream_returns_direct_children(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """get_downstream 返回直接下游 artifact 列表。"""
        actor = _actor(roles=["ADMIN"])
        parent = await _upload(artifact_service, content=_content("p"), actor=actor)
        c1 = await _upload(artifact_service, content=_content("c1"), actor=actor)
        c2 = await _upload(artifact_service, content=_content("c2"), actor=actor)

        await schema_service.record_lineage(
            c1, parent_artifact_ids=[parent],
            actor_id=actor["user_id"], actor=actor,
        )
        await schema_service.record_lineage(
            c2, parent_artifact_ids=[parent],
            actor_id=actor["user_id"], actor=actor,
        )

        downstream = await schema_service.get_downstream(
            parent, actor_id=actor["user_id"], actor=actor,
        )
        downstream_ids = {d["id"] for d in downstream}
        assert downstream_ids == {c1, c2}

    @pytest.mark.asyncio
    async def test_get_lineage_returns_full_graph(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """get_lineage 返回完整上游 + 下游 + 边。"""
        actor = _actor(roles=["ADMIN"])
        # 构造图: gp → p → root → c1, c2
        gp = await _upload(artifact_service, content=_content("gp"), actor=actor)
        p = await _upload(artifact_service, content=_content("p"), actor=actor)
        root = await _upload(artifact_service, content=_content("root"), actor=actor)
        c1 = await _upload(artifact_service, content=_content("c1"), actor=actor)
        c2 = await _upload(artifact_service, content=_content("c2"), actor=actor)

        await schema_service.record_lineage(
            p, parent_artifact_ids=[gp],
            actor_id=actor["user_id"], actor=actor,
        )
        await schema_service.record_lineage(
            root, parent_artifact_ids=[p],
            actor_id=actor["user_id"], actor=actor,
        )
        await schema_service.record_lineage(
            c1, parent_artifact_ids=[root],
            actor_id=actor["user_id"], actor=actor,
        )
        await schema_service.record_lineage(
            c2, parent_artifact_ids=[root],
            actor_id=actor["user_id"], actor=actor,
        )

        graph = await schema_service.get_lineage(
            root, actor_id=actor["user_id"], actor=actor,
        )
        assert graph["artifact_id"] == root

        upstream_ids = {u["id"] for u in graph["upstream"]}
        downstream_ids = {d["id"] for d in graph["downstream"]}
        # upstream 含 gp 与 p（不含 root 自身）
        assert upstream_ids == {gp, p}
        # downstream 含 c1 与 c2（不含 root 自身）
        assert downstream_ids == {c1, c2}
        # edges 至少覆盖 root 的 parent 边与 child 边
        edge_pairs = {
            (e["artifact_id"], e["parent_artifact_id"]) for e in graph["edges"]
        }
        assert (root, p) in edge_pairs
        assert (c1, root) in edge_pairs
        assert (c2, root) in edge_pairs

    @pytest.mark.asyncio
    async def test_get_lineage_not_found(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """get_lineage 不存在的 artifact 抛 NotFoundError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(NotFoundError):
            await schema_service.get_lineage(
                "nonexistent", actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_get_upstream_no_parents_returns_empty(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """无上游的 artifact get_upstream 返回空列表。"""
        actor = _actor(roles=["ADMIN"])
        a_id = await _upload(artifact_service, actor=actor)
        upstream = await schema_service.get_upstream(
            a_id, actor_id=actor["user_id"], actor=actor,
        )
        assert upstream == []


# --------------------------------------------------------------------------- #
# 验收 3（续）：禁止成环与跨项目泄露
# --------------------------------------------------------------------------- #


class TestLineageConstraints:
    """血缘约束测试：环检测与跨项目拒绝。"""

    @pytest.mark.asyncio
    async def test_cycle_detection_direct_back_edge(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """A→B 后 B→A 抛 UnsupportedOperationError（成环）。"""
        actor = _actor(roles=["ADMIN"])
        a = await _upload(artifact_service, content=_content("a"), actor=actor)
        b = await _upload(artifact_service, content=_content("b"), actor=actor)

        # a -> b（a 的 parent 是 b）
        await schema_service.record_lineage(
            a, parent_artifact_ids=[b],
            actor_id=actor["user_id"], actor=actor,
        )
        # b -> a 会形成环 a→b→a
        with pytest.raises(UnsupportedOperationError, match="环"):
            await schema_service.record_lineage(
                b, parent_artifact_ids=[a],
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_cycle_detection_transitive_back_edge(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """A→B→C 后 C→A 抛 UnsupportedOperationError（间接成环）。"""
        actor = _actor(roles=["ADMIN"])
        a = await _upload(artifact_service, content=_content("a"), actor=actor)
        b = await _upload(artifact_service, content=_content("b"), actor=actor)
        c = await _upload(artifact_service, content=_content("c"), actor=actor)

        # a -> b -> c
        await schema_service.record_lineage(
            a, parent_artifact_ids=[b],
            actor_id=actor["user_id"], actor=actor,
        )
        await schema_service.record_lineage(
            b, parent_artifact_ids=[c],
            actor_id=actor["user_id"], actor=actor,
        )
        # c -> a 会形成 a→b→c→a
        with pytest.raises(UnsupportedOperationError, match="环"):
            await schema_service.record_lineage(
                c, parent_artifact_ids=[a],
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_cross_project_lineage_rejected(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """跨项目血缘抛 UnsupportedOperationError。"""
        actor = _actor(roles=["ADMIN"])
        parent = await _upload(
            artifact_service, project_id="proj-alpha",
            content=_content("parent"), actor=actor,
        )
        child = await _upload(
            artifact_service, project_id="proj-beta",
            content=_content("child"), actor=actor,
        )

        with pytest.raises(UnsupportedOperationError, match="跨项目"):
            await schema_service.record_lineage(
                child, parent_artifact_ids=[parent],
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_same_project_lineage_allowed(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """同项目血缘允许。"""
        actor = _actor(roles=["ADMIN"])
        parent = await _upload(
            artifact_service, project_id="proj-same",
            content=_content("parent"), actor=actor,
        )
        child = await _upload(
            artifact_service, project_id="proj-same",
            content=_content("child"), actor=actor,
        )

        edges = await schema_service.record_lineage(
            child, parent_artifact_ids=[parent],
            actor_id=actor["user_id"], actor=actor,
        )
        assert len(edges) == 1


# --------------------------------------------------------------------------- #
# 验收 4：Diff —— line / field / identical
# --------------------------------------------------------------------------- #


class TestArtifactDiff:
    """``diff_artifacts`` 集成测试。"""

    @pytest.mark.asyncio
    async def test_diff_identical_artifacts(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """相同内容（content_hash 相等）→ identical=True，entries 为空。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("identical-content")
        a = await _upload(artifact_service, content=content, actor=actor)
        b = await _upload(artifact_service, content=content, actor=actor)

        diff = await schema_service.diff_artifacts(
            a, b, actor_id=actor["user_id"], actor=actor,
        )
        assert diff["identical"] is True
        assert diff["diff_kind"] == "none"
        assert diff["entries"] == []
        assert diff["content_hash_a"] == diff["content_hash_b"]

    @pytest.mark.asyncio
    async def test_diff_json_field_diff(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """两侧都是 JSON → field diff，含 added/removed/modified。"""
        actor = _actor(roles=["ADMIN"])
        a = await _upload(
            artifact_service,
            content=_json_content({"name": "x", "version": 1, "shared": "old"}),
            actor=actor,
        )
        b = await _upload(
            artifact_service,
            content=_json_content({"name": "y", "version": 1, "added": "new"}),
            actor=actor,
        )

        diff = await schema_service.diff_artifacts(
            a, b, actor_id=actor["user_id"], actor=actor,
        )
        assert diff["identical"] is False
        assert diff["diff_kind"] == "field"
        # entries 按 (key, type) 排序
        keys = {(e["key"], e["type"]) for e in diff["entries"]}
        # $.name modified, $.shared removed, $.added added
        assert ("$.name", "modified") in keys
        assert ("$.shared", "removed") in keys
        assert ("$.added", "added") in keys

    @pytest.mark.asyncio
    async def test_diff_text_line_diff(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """两侧都不是 JSON → line diff。"""
        actor = _actor(roles=["ADMIN"])
        a = await _upload(
            artifact_service,
            content=b"line1\nline2\nline3\n",
            actor=actor,
        )
        b = await _upload(
            artifact_service,
            content=b"line1\nline2-changed\nline3\nline4\n",
            actor=actor,
        )

        diff = await schema_service.diff_artifacts(
            a, b, actor_id=actor["user_id"], actor=actor,
        )
        assert diff["identical"] is False
        assert diff["diff_kind"] == "line"
        # 至少有一个 modified 行（line2 -> line2-changed）
        modified = [e for e in diff["entries"] if e["type"] == "modified"]
        assert len(modified) >= 1
        # 至少有一个 added 行（line4）
        added = [e for e in diff["entries"] if e["type"] == "added"]
        assert len(added) >= 1

    @pytest.mark.asyncio
    async def test_diff_artifact_not_found(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """任一 artifact 不存在抛 NotFoundError。"""
        actor = _actor(roles=["ADMIN"])
        a = await _upload(artifact_service, actor=actor)
        with pytest.raises(NotFoundError):
            await schema_service.diff_artifacts(
                a, "nonexistent", actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_diff_deterministic_order(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """同一对 artifact 多次 diff 结果一致（entries 按 key 排序）。"""
        actor = _actor(roles=["ADMIN"])
        a = await _upload(
            artifact_service,
            content=_json_content({"z": 1, "a": 2, "m": 3}),
            actor=actor,
        )
        b = await _upload(
            artifact_service,
            content=_json_content({"z": 99, "a": 2, "m": 3}),
            actor=actor,
        )

        diff1 = await schema_service.diff_artifacts(
            a, b, actor_id=actor["user_id"], actor=actor,
        )
        diff2 = await schema_service.diff_artifacts(
            a, b, actor_id=actor["user_id"], actor=actor,
        )
        assert diff1["entries"] == diff2["entries"]
        # entries 按 key 排序
        keys = [e["key"] for e in diff1["entries"]]
        assert keys == sorted(keys)

    @pytest.mark.asyncio
    async def test_diff_hash_immutable(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
        db: Database,
    ) -> None:
        """diff_artifacts 不修改任一 artifact 的 content_hash。"""
        actor = _actor(roles=["ADMIN"])
        ca = _json_content({"name": "a"})
        cb = _json_content({"name": "b"})
        a = await _upload(artifact_service, content=ca, actor=actor)
        b = await _upload(artifact_service, content=cb, actor=actor)
        hash_a = hashlib.sha256(ca).hexdigest()
        hash_b = hashlib.sha256(cb).hexdigest()

        await schema_service.diff_artifacts(
            a, b, actor_id=actor["user_id"], actor=actor,
        )

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT content_hash FROM artifacts WHERE id IN (?, ?)",
                (a, b),
            ) as cur:
                rows = await cur.fetchall()
        hashes = {r[0] for r in rows}
        assert hashes == {hash_a, hash_b}


# --------------------------------------------------------------------------- #
# 验收 5：内容 hash 不可变 —— 所有 schema/lineage/diff 操作不修改 content_hash
# --------------------------------------------------------------------------- #


class TestContentHashImmutability:
    """内容 hash 不可变测试（验收 5）。"""

    @pytest.mark.asyncio
    async def test_lineage_does_not_modify_hash(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
        db: Database,
    ) -> None:
        """record_lineage / get_lineage 不修改 content_hash。"""
        actor = _actor(roles=["ADMIN"])
        parent_content = _content("lineage-parent-immutable")
        child_content = _content("lineage-child-immutable")
        parent = await _upload(
            artifact_service, content=parent_content, actor=actor,
        )
        child = await _upload(
            artifact_service, content=child_content, actor=actor,
        )
        parent_hash = hashlib.sha256(parent_content).hexdigest()
        child_hash = hashlib.sha256(child_content).hexdigest()

        await schema_service.record_lineage(
            child, parent_artifact_ids=[parent],
            actor_id=actor["user_id"], actor=actor,
        )
        await schema_service.get_lineage(
            child, actor_id=actor["user_id"], actor=actor,
        )
        await schema_service.get_upstream(
            child, actor_id=actor["user_id"], actor=actor,
        )
        await schema_service.get_downstream(
            parent, actor_id=actor["user_id"], actor=actor,
        )

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT id, content_hash FROM artifacts WHERE id IN (?, ?)",
                (parent, child),
            ) as cur:
                rows = await cur.fetchall()
        hash_map = {r[0]: r[1] for r in rows}
        assert hash_map[parent] == parent_hash
        assert hash_map[child] == child_hash

    @pytest.mark.asyncio
    async def test_schema_registration_does_not_touch_artifacts(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
        db: Database,
    ) -> None:
        """register_schema / deprecate_schema 不修改 artifact 行。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("schema-immutable")
        a_id = await _upload(artifact_service, content=content, actor=actor)
        original_hash = hashlib.sha256(content).hexdigest()

        await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )
        await schema_service.deprecate_schema(
            "task_payload", 1,
            actor_id=actor["user_id"], actor=actor,
        )

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT content_hash FROM artifacts WHERE id = ?",
                (a_id,),
            ) as cur:
                row = await cur.fetchone()
        assert row[0] == original_hash


# --------------------------------------------------------------------------- #
# 验收 6：权限检查
# --------------------------------------------------------------------------- #


class TestSchemaPermissions:
    """权限检查测试（验收 6）。"""

    @pytest.mark.asyncio
    async def test_observer_can_read_schema_but_not_write(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """OBSERVER 可读 Schema 但不可注册/废弃。"""
        admin = _actor(roles=["ADMIN"])
        observer = _actor(user_id="obs", roles=["OBSERVER"])

        # admin 注册
        await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=admin["user_id"], actor=admin,
        )

        # observer 可读
        view = await schema_service.get_schema(
            "task_payload", 1,
            actor_id=observer["user_id"], actor=observer,
        )
        assert view["schema_name"] == "task_payload"

        # observer 不可注册
        with pytest.raises(PermissionDeniedError):
            await schema_service.register_schema(
                "task_payload", 2, _simple_schema(),
                actor_id=observer["user_id"], actor=observer,
            )

        # observer 不可废弃
        with pytest.raises(PermissionDeniedError):
            await schema_service.deprecate_schema(
                "task_payload", 1,
                actor_id=observer["user_id"], actor=observer,
            )

    @pytest.mark.asyncio
    async def test_designer_can_register_schema(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """DESIGNER 可注册/废弃 Schema（DESIGNER/ADMIN 才能注册）。"""
        designer = _actor(user_id="designer", roles=["DESIGNER"])

        view = await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=designer["user_id"], actor=designer,
        )
        assert view["status"] == "ACTIVE"

        deprecated = await schema_service.deprecate_schema(
            "task_payload", 1,
            actor_id=designer["user_id"], actor=designer,
        )
        assert deprecated["status"] == "DEPRECATED"

    @pytest.mark.asyncio
    async def test_approver_can_read_but_not_write_schema(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """APPROVER 可读但不可写 Schema。"""
        admin = _actor(roles=["ADMIN"])
        approver = _actor(user_id="approver", roles=["APPROVER"])

        await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=admin["user_id"], actor=admin,
        )

        # APPROVER 可读
        view = await schema_service.get_schema(
            "task_payload", 1,
            actor_id=approver["user_id"], actor=approver,
        )
        assert view is not None

        # APPROVER 不可写
        with pytest.raises(PermissionDeniedError):
            await schema_service.register_schema(
                "task_payload", 2, _simple_schema(),
                actor_id=approver["user_id"], actor=approver,
            )

    @pytest.mark.asyncio
    async def test_observer_cannot_record_lineage(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """OBSERVER 无 write artifacts 权限，不能记录血缘。"""
        admin = _actor(roles=["ADMIN"])
        observer = _actor(user_id="obs", roles=["OBSERVER"])
        parent = await _upload(artifact_service, actor=admin)
        child = await _upload(artifact_service, actor=admin)

        with pytest.raises(PermissionDeniedError):
            await schema_service.record_lineage(
                child, parent_artifact_ids=[parent],
                actor_id=observer["user_id"], actor=observer,
            )

    @pytest.mark.asyncio
    async def test_observer_can_query_lineage(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """OBSERVER 有 read artifacts 权限，可查询血缘。"""
        admin = _actor(roles=["ADMIN"])
        observer = _actor(user_id="obs", roles=["OBSERVER"])
        parent = await _upload(artifact_service, actor=admin)
        child = await _upload(artifact_service, actor=admin)

        await schema_service.record_lineage(
            child, parent_artifact_ids=[parent],
            actor_id=admin["user_id"], actor=admin,
        )

        graph = await schema_service.get_lineage(
            child, actor_id=observer["user_id"], actor=observer,
        )
        assert graph["artifact_id"] == child

        upstream = await schema_service.get_upstream(
            child, actor_id=observer["user_id"], actor=observer,
        )
        assert {u["id"] for u in upstream} == {parent}

    @pytest.mark.asyncio
    async def test_no_roles_denied(
        self, schema_service: ArtifactSchemaServiceImpl
    ) -> None:
        """无角色用户被拒绝。"""
        actor = _actor(user_id="nobody", roles=[])
        with pytest.raises(PermissionDeniedError):
            await schema_service.get_schema(
                "any", 1, actor_id=actor["user_id"], actor=actor,
            )


# --------------------------------------------------------------------------- #
# 验收 7：事件经 OutboxRepository 写入
# --------------------------------------------------------------------------- #


class TestSchemaEvents:
    """事件测试：``artifact.schema_registered``、``artifact.schema_deprecated``、
    ``artifact.lineage_recorded``。
    """

    @pytest.mark.asyncio
    async def test_register_schema_writes_event(
        self,
        schema_service: ArtifactSchemaServiceImpl,
        db: Database,
    ) -> None:
        """register_schema 后 outbox_events 含 artifact.schema_registered 事件。"""
        actor = _actor(roles=["DESIGNER"])
        await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )

        events = await self._list_events_by_type(
            db, "artifact.schema_registered"
        )
        assert len(events) == 1
        # 列顺序: (id, event_type, aggregate_type, aggregate_id, project_id, payload)
        evt = events[0]
        assert evt[2] == "artifact"  # aggregate_type
        assert evt[3] == "task_payload:v1"  # aggregate_id
        payload = json.loads(evt[5])
        assert payload["schema_name"] == "task_payload"
        assert payload["version"] == 1
        assert payload["status"] == "ACTIVE"

    @pytest.mark.asyncio
    async def test_deprecate_schema_writes_event(
        self,
        schema_service: ArtifactSchemaServiceImpl,
        db: Database,
    ) -> None:
        """deprecate_schema 后 outbox_events 含 artifact.schema_deprecated 事件。"""
        actor = _actor(roles=["DESIGNER"])
        await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )
        await schema_service.deprecate_schema(
            "task_payload", 1,
            actor_id=actor["user_id"], actor=actor,
        )

        events = await self._list_events_by_type(
            db, "artifact.schema_deprecated"
        )
        assert len(events) == 1
        evt = events[0]
        assert evt[3] == "task_payload:v1"  # aggregate_id
        payload = json.loads(evt[5])
        assert payload["schema_name"] == "task_payload"
        assert payload["version"] == 1

    @pytest.mark.asyncio
    async def test_record_lineage_writes_event(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
        db: Database,
    ) -> None:
        """record_lineage 后 outbox_events 含 artifact.lineage_recorded 事件。"""
        actor = _actor(roles=["ADMIN"])
        parent = await _upload(
            artifact_service, content=_content("event-parent"), actor=actor,
        )
        child = await _upload(
            artifact_service, content=_content("event-child"), actor=actor,
        )

        await schema_service.record_lineage(
            child, parent_artifact_ids=[parent],
            transformation="event-test",
            actor_id=actor["user_id"], actor=actor,
        )

        # lineage 事件 project_id 是 child 所在项目
        repo = SqliteOutboxRepository(db)
        events = await repo.find_by_project("proj-001")
        lineage_events = [
            e for e in events if e.event_type == "artifact.lineage_recorded"
        ]
        assert len(lineage_events) == 1
        evt = lineage_events[0]
        assert evt.aggregate_id == child
        assert evt.payload["parent_artifact_ids"] == [parent]
        assert evt.payload["relation"] == "DERIVED_FROM"
        assert evt.payload["transformation"] == "event-test"
        assert evt.payload["inserted_count"] == 1

    @pytest.mark.asyncio
    async def test_record_lineage_idempotent_no_new_event(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
        db: Database,
    ) -> None:
        """幂等 record_lineage（所有边已存在）不写入新事件。"""
        actor = _actor(roles=["ADMIN"])
        parent = await _upload(artifact_service, actor=actor)
        child = await _upload(artifact_service, actor=actor)

        await schema_service.record_lineage(
            child, parent_artifact_ids=[parent],
            actor_id=actor["user_id"], actor=actor,
        )
        # 第二次相同 (child, parent, relation) —— inserted_count=0，不写事件
        await schema_service.record_lineage(
            child, parent_artifact_ids=[parent],
            actor_id=actor["user_id"], actor=actor,
        )

        repo = SqliteOutboxRepository(db)
        events = await repo.find_by_project("proj-001")
        lineage_events = [
            e for e in events if e.event_type == "artifact.lineage_recorded"
        ]
        assert len(lineage_events) == 1  # 仍只有一条

    @pytest.mark.asyncio
    async def test_event_same_transaction_as_metadata(
        self,
        schema_service: ArtifactSchemaServiceImpl,
        db: Database,
    ) -> None:
        """事件与元数据在同事务提交：commit 后均可见。"""
        actor = _actor(roles=["DESIGNER"])
        await schema_service.register_schema(
            "task_payload", 1, _simple_schema(),
            actor_id=actor["user_id"], actor=actor,
        )

        # schema 元数据可见
        view = await schema_service.get_schema(
            "task_payload", 1,
            actor_id=actor["user_id"], actor=actor,
        )
        assert view["schema_name"] == "task_payload"

        # 事件也可见
        events = await self._list_events_by_type(
            db, "artifact.schema_registered"
        )
        assert len(events) == 1

    @staticmethod
    async def _list_events_by_type(
        db: Database, event_type: str
    ) -> list[tuple]:
        """直接从 outbox_events 表查询指定类型的事件行。

        schema_registered/schema_deprecated 事件 ``project_id`` 为 NULL，
        无法用 ``SqliteOutboxRepository.find_by_project`` 查询，所以直接 SQL。
        """
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT id, event_type, aggregate_type, aggregate_id, "
                "project_id, payload "
                "FROM outbox_events WHERE event_type = ? "
                "ORDER BY occurred_at ASC, rowid ASC",
                (event_type,),
            ) as cur:
                rows = await cur.fetchall()
        return [tuple(r) for r in rows]


# --------------------------------------------------------------------------- #
# 验收 8：保留 TASK-011 的 SchemaLoader
# --------------------------------------------------------------------------- #


class TestSchemaLoaderPreserved:
    """确保 TASK-011 的 SchemaLoader 仍可正常导入与使用。"""

    @pytest.mark.asyncio
    async def test_schema_loader_importable(self) -> None:
        """``maf_server.git_coordination.schemas.SchemaLoader`` 仍可导入。"""
        from maf_artifact_schemas.protocol import SchemaRef
        from maf_server.git_coordination.schemas import SchemaLoader

        loader = SchemaLoader()
        # 加载内置 templates/git_coordination/schemas/project-v1.schema.json
        # SchemaLoader 使用 ``get_schema(ref: SchemaRef)`` API（TASK-011）。
        schema = loader.get_schema(SchemaRef(name="project", version=1))
        assert schema is not None
        assert "$id" in schema or "type" in schema

    @pytest.mark.asyncio
    async def test_artifact_schemas_protocol_preserved(self) -> None:
        """``maf_artifact_schemas.protocol`` 仍保留 ProtocolVersion / SchemaRef。"""
        from maf_artifact_schemas.protocol import (
            KNOWN_LINEAGE_RELATIONS,
            ProtocolVersion,
            SchemaRef,
            SchemaValidationResult,
            validate_lineage_relation,
            validate_schema_name,
            validate_schema_version,
        )

        # 原有类型仍存在
        assert ProtocolVersion.latest() == ProtocolVersion.V1
        ref = SchemaRef(name="task", version=1)
        assert ref.file_stem == "task-v1"

        # TASK-079 新增校验函数
        assert validate_schema_name("task_payload") == "task_payload"
        assert validate_schema_version(1) == 1
        assert validate_lineage_relation("DERIVED_FROM") == "DERIVED_FROM"
        assert "SUPERSEDES" in KNOWN_LINEAGE_RELATIONS


# --------------------------------------------------------------------------- #
# 验收 9：router 集成（端到端）
# --------------------------------------------------------------------------- #


class TestRouterEndpoints:
    """``build_artifact_router`` 注册 Schema/lineage/diff 端点测试。"""

    @staticmethod
    def _collect_paths(router: Any) -> set[str]:
        """从 FastAPI router 提取所有已注册路径。

        FastAPI 0.139+ 的 ``include_router`` 会将子 router 包装为
        ``_IncludedRouter``（无 ``path`` 属性），需要展开其 ``original_router``
        的子路由。直接路由（``APIRoute``）保持 ``path`` 访问。
        """
        paths: set[str] = set()
        for rt in router.routes:
            if hasattr(rt, "path"):
                paths.add(rt.path)
            elif hasattr(rt, "original_router"):
                for srt in rt.original_router.routes:
                    if hasattr(srt, "path"):
                        paths.add(srt.path)
        return paths

    def test_router_registers_schema_endpoints(
        self,
        artifact_service: ArtifactServiceImpl,
        schema_service: ArtifactSchemaServiceImpl,
    ) -> None:
        """传入 schema_service 后 router 含 artifact-schemas 路由。"""
        from maf_server.modules.artifacts.router import build_artifact_router

        router = build_artifact_router(artifact_service, schema_service)
        paths = self._collect_paths(router)
        # schema 端点
        assert "/api/v1/artifact-schemas" in paths
        assert "/api/v1/artifact-schemas/{schema_name}/{version}" in paths
        assert (
            "/api/v1/artifact-schemas/{schema_name}/{version}/deprecate"
            in paths
        )
        # artifact 级端点
        assert "/api/v1/artifacts/{artifact_id}/validate" in paths
        assert "/api/v1/artifacts/{artifact_id}/lineage" in paths
        assert "/api/v1/artifacts/{artifact_id}/upstream" in paths
        assert "/api/v1/artifacts/{artifact_id}/downstream" in paths
        assert "/api/v1/artifacts/{artifact_id}/diff" in paths

    def test_router_without_schema_service_skips_endpoints(
        self,
        artifact_service: ArtifactServiceImpl,
    ) -> None:
        """不传 schema_service 时不注册 schema/lineage/diff 端点（向后兼容）。"""
        from maf_server.modules.artifacts.router import build_artifact_router

        router = build_artifact_router(artifact_service)
        paths = self._collect_paths(router)
        # TASK-078 端点仍存在
        assert "/api/v1/artifacts" in paths
        assert "/api/v1/artifacts/{artifact_id}" in paths
        # TASK-079 端点不存在
        assert "/api/v1/artifact-schemas" not in paths
        assert "/api/v1/artifacts/{artifact_id}/validate" not in paths
        assert "/api/v1/artifacts/{artifact_id}/lineage" not in paths
        assert "/api/v1/artifacts/{artifact_id}/diff" not in paths
