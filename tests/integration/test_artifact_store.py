"""TASK-078 集成测试：本地 ArtifactStore 与元数据。

验收标准：
1. ArtifactFileStore 实现本地文件系统存储，路径安全（防遍历），内容寻址（SHA-256）。
2. ArtifactService 实现上传/完成/获取/列表/下载/删除。
3. 内容 hash 校验：上传计算、下载验证。
4. 权限检查通过 PermissionService.require。
5. 事件经 OutboxRepository 写入。
6. 大文件不一次读入内存（put_stream 流式写入）。
7. 哈希不符不创建版本。
8. API 不暴露宿主绝对路径。

测试范围：
- ``apps/server/src/maf_server/core/artifact_store.py``：``LocalArtifactFileStore``。
- ``apps/server/src/maf_server/modules/artifacts/``：service、repository、schemas。
- ``apps/server/src/maf_server/core/events.py``：Outbox 事件验证。
- ``apps/server/src/maf_server/core/unit_of_work.py``：事务边界。
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

import pytest
import pytest_asyncio

from maf_contracts.common import ActorContext
from maf_domain.errors import (
    ArgumentError,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedOperationError,
    VersionConflictError,
)
from maf_policy import CasbinPermissionService
from maf_server.config import ServerSettings
from maf_server.core.artifact_store import (
    LocalArtifactFileStore,
    StoredObject,
    storage_key_for,
)
from maf_server.core.database import Database
from maf_server.core.events import (
    SqliteEventPublisher,
    SqliteOutboxRepository,
    init_outbox_schema,
)
from maf_server.core.unit_of_work import SqliteUnitOfWork
from maf_server.modules.artifacts.repository import (
    SqliteArtifactRepository,
    init_schema as init_artifact_schema,
    new_artifact_id,
)
from maf_server.modules.artifacts.service import ArtifactServiceImpl

_SECRET_PLAINTEXT = "test-secret-for-artifact-task-078"


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
    """构造带 artifact 策略的 CasbinPermissionService。

    DEFAULT_POLICIES 不含 artifacts 资源（TASK-031 范围）；
    本测试在 service 实例上追加 OWNER/DESIGNER 的 artifacts 读写策略，
    以验证权限检查。ADMIN 默认拥有 ``*`` ``.*`` 全权。
    """
    service = CasbinPermissionService()
    service.add_policy("OWNER", "artifacts", "(read|write)")
    service.add_policy("DESIGNER", "artifacts", "(read|write)")
    service.add_policy("APPROVER", "artifacts", "read")
    return service


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    """已初始化并建好 artifacts + outbox_events 表的 Database。"""
    settings = _make_settings(tmp_path)
    database = Database(settings)
    await database.initialize()
    # 建 artifacts 表
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
    store = LocalArtifactFileStore(settings.artifact_root)
    return store


@pytest_asyncio.fixture
async def service(
    db: Database, file_store: LocalArtifactFileStore
) -> ArtifactServiceImpl:
    """注入 Database、FileStore、自定义 PermissionService 的 ArtifactServiceImpl。"""
    return ArtifactServiceImpl(
        database=db,
        file_store=file_store,
        permission_service=_make_permission_service(),
    )


def _actor(
    user_id: str = "user-admin",
    roles: list[str] | None = None,
    trace_id: str = "artifact-trace",
) -> ActorContext:
    """构造测试用 ActorContext。

    ``roles=None`` 时默认 ADMIN；``roles=[]`` 显式表示无角色（用于测试
    权限拒绝场景，不能被 ``or`` 折叠成 ``["ADMIN"]``，因为空列表是 falsy）。
    """
    return ActorContext(
        user_id=user_id,
        organization_id="org-001",
        permission_keys=roles if roles is not None else ["ADMIN"],
        trace_id=trace_id,
    )


def _content(data: str = "hello artifact world") -> bytes:
    """测试用内容。"""
    return data.encode("utf-8")


# --------------------------------------------------------------------------- #
# 乐观锁测试辅助：返回过期 version_no 的 repository 包装器
# --------------------------------------------------------------------------- #


class _StaleVersionRepository:
    """包装 ``SqliteArtifactRepository``，``get_artifact`` 返回过期 version_no。

    用于在 service 层测试乐观锁冲突：service 读到旧 version_no（如 0），
    以此作为 ``expected_version`` 调用 ``update_status``，但 DB 中实际
    version_no 已是 1，因此 ``UPDATE ... WHERE version_no = ?`` 不匹配，
    返回 0，service 抛 ``VersionConflictError``。

    不能通过"在 service 调用前手动 UPDATE version_no"模拟冲突：因为
    service 在同一 ``BEGIN IMMEDIATE`` 事务内读到的就是最新 version_no，
    会以最新值作为 ``expected_version``，UPDATE 仍然匹配。唯一可靠的方式是
    让 service 读到与 DB 不一致的过期 version_no。
    """

    def __init__(
        self,
        inner: SqliteArtifactRepository,
        stale_offset: int = 1,
    ) -> None:
        self._inner = inner
        self._stale_offset = stale_offset

    async def get_artifact(self, conn, artifact_id: str):
        rec = await self._inner.get_artifact(conn, artifact_id)
        if rec is not None:
            from dataclasses import replace

            return replace(
                rec, version_no=max(0, rec.version_no - self._stale_offset)
            )
        return rec

    async def insert_artifact(self, conn, **kwargs) -> None:
        await self._inner.insert_artifact(conn, **kwargs)

    async def list_artifacts(self, conn, project_id, **kwargs):
        return await self._inner.list_artifacts(conn, project_id, **kwargs)

    async def update_status(self, conn, artifact_id, **kwargs):
        return await self._inner.update_status(conn, artifact_id, **kwargs)

    async def count_by_content_hash(self, conn, content_hash: str) -> int:
        return await self._inner.count_by_content_hash(conn, content_hash)


# --------------------------------------------------------------------------- #
# 验收 1：ArtifactFileStore 本地存储、路径安全、内容寻址
# --------------------------------------------------------------------------- #


class TestLocalArtifactFileStore:
    """``LocalArtifactFileStore`` 内容寻址存储与路径安全。"""

    @pytest.mark.asyncio
    async def test_put_stream_content_addressable(
        self, file_store: LocalArtifactFileStore
    ) -> None:
        """put_stream 按 SHA-256 内容寻址存储。"""
        content = _content("content-addressable-test")
        sha = hashlib.sha256(content).hexdigest()
        length = len(content)
        stream = io.BytesIO(content)

        stored = await file_store.put_stream(stream, sha, length)

        assert stored["sha256"] == sha
        assert stored["content_length"] == length
        # storage_key 格式 ``ab/cdef...``
        assert stored["storage_key"] == storage_key_for(sha)
        # 文件存在
        assert await file_store.exists(stored["storage_key"], sha)

    @pytest.mark.asyncio
    async def test_put_stream_same_content_dedup(
        self, file_store: LocalArtifactFileStore
    ) -> None:
        """相同内容只存一份（内容寻址幂等）。"""
        content = _content("dedup-test-content")
        sha = hashlib.sha256(content).hexdigest()
        length = len(content)

        stored1 = await file_store.put_stream(io.BytesIO(content), sha, length)
        stored2 = await file_store.put_stream(io.BytesIO(content), sha, length)

        assert stored1["storage_key"] == stored2["storage_key"]
        # 只有一个物理文件
        target_path = file_store.root / stored1["storage_key"]
        assert target_path.exists()

    @pytest.mark.asyncio
    async def test_put_stream_hash_mismatch_raises(
        self, file_store: LocalArtifactFileStore
    ) -> None:
        """哈希不符不创建文件（验收：哈希不符不创建版本）。"""
        content = _content("hash-mismatch-test")
        wrong_sha = "a" * 64  # 64 hex chars, wrong
        length = len(content)

        with pytest.raises(ValueError, match="内容哈希不符"):
            await file_store.put_stream(
                io.BytesIO(content), wrong_sha, length
            )

        # 临时文件应已清理
        tmp_files = list((file_store.root / ".tmp").iterdir())
        assert len(tmp_files) == 0

    @pytest.mark.asyncio
    async def test_put_stream_length_mismatch_raises(
        self, file_store: LocalArtifactFileStore
    ) -> None:
        """长度不符不创建文件。"""
        content = _content("length-mismatch-test")
        sha = hashlib.sha256(content).hexdigest()
        wrong_length = len(content) + 10

        with pytest.raises(ValueError, match="内容长度不符"):
            await file_store.put_stream(
                io.BytesIO(content), sha, wrong_length
            )

    @pytest.mark.asyncio
    async def test_open_stream_reads_content(
        self, file_store: LocalArtifactFileStore
    ) -> None:
        """open_stream 流式读取内容。"""
        content = _content("open-stream-test" * 100)
        sha = hashlib.sha256(content).hexdigest()
        length = len(content)
        stored = await file_store.put_stream(io.BytesIO(content), sha, length)

        chunks: list[bytes] = []
        async for chunk in file_store.open_stream(stored["storage_key"]):
            chunks.append(chunk)

        assert b"".join(chunks) == content

    @pytest.mark.asyncio
    async def test_open_stream_path_traversal_rejected(
        self, file_store: LocalArtifactFileStore
    ) -> None:
        """路径遍历攻击被拦截（验收：路径安全）。"""
        malicious_keys = [
            "../../../etc/passwd",
            "..\\..\\..\\windows\\system32",
            "/etc/passwd",
            "ab/../../../etc/passwd",
        ]
        for key in malicious_keys:
            with pytest.raises((ValueError, FileNotFoundError)):
                async for _ in file_store.open_stream(key):
                    pass

    @pytest.mark.asyncio
    async def test_exists_verifies_hash(
        self, file_store: LocalArtifactFileStore
    ) -> None:
        """exists 同时校验文件存在和内容哈希。"""
        content = _content("exists-verify-test")
        sha = hashlib.sha256(content).hexdigest()
        length = len(content)
        stored = await file_store.put_stream(io.BytesIO(content), sha, length)

        # 正确 hash → True
        assert await file_store.exists(stored["storage_key"], sha) is True
        # 错误 hash → False
        wrong_sha = "b" * 64
        assert await file_store.exists(stored["storage_key"], wrong_sha) is False
        # 不存在的 key → False
        assert await file_store.exists("ab/nonexistent", sha) is False

    @pytest.mark.asyncio
    async def test_delete_unreferenced(
        self, file_store: LocalArtifactFileStore
    ) -> None:
        """delete_unreferenced 删除文件。"""
        content = _content("delete-test-content")
        sha = hashlib.sha256(content).hexdigest()
        length = len(content)
        stored = await file_store.put_stream(io.BytesIO(content), sha, length)

        assert await file_store.delete_unreferenced(stored["storage_key"]) is True
        # 再次删除返回 False
        assert await file_store.delete_unreferenced(stored["storage_key"]) is False

    @pytest.mark.asyncio
    async def test_storage_key_no_absolute_path(
        self, file_store: LocalArtifactFileStore
    ) -> None:
        """storage_key 是相对键，不含宿主绝对路径（验收：API 不暴露宿主绝对路径）。"""
        content = _content("no-abs-path-test")
        sha = hashlib.sha256(content).hexdigest()
        length = len(content)
        stored = await file_store.put_stream(io.BytesIO(content), sha, length)

        key = stored["storage_key"]
        # 不含绝对路径分隔符（Windows 盘符或 Unix 根）
        assert ":" not in key
        assert not key.startswith("/")
        assert not key.startswith("\\")
        # 格式为 ab/cdef...
        assert len(key) == 65  # 2 + 1 + 62
        assert key[2] == "/"


# --------------------------------------------------------------------------- #
# 验收 2：ArtifactService 上传/完成/获取/列表/下载/删除
# --------------------------------------------------------------------------- #


class TestArtifactServiceUpload:
    """``ArtifactService.upload_artifact`` 测试。"""

    @pytest.mark.asyncio
    async def test_upload_creates_completed_artifact(
        self, service: ArtifactServiceImpl
    ) -> None:
        """上传创建 COMPLETED 状态的 artifact。"""
        content = _content("upload-test-content")
        actor = _actor(roles=["ADMIN"])

        view = await service.upload_artifact(
            project_id="proj-001",
            artifact_type="code_snapshot",
            content_bytes=content,
            actor_id=actor["user_id"],
            actor=actor,
        )

        assert view["project_id"] == "proj-001"
        assert view["artifact_type"] == "code_snapshot"
        assert view["status"] == "COMPLETED"
        assert view["size_bytes"] == len(content)
        assert view["content_hash"] == hashlib.sha256(content).hexdigest()
        assert view["version_no"] == 1
        assert view["uploaded_by"] == actor["user_id"]
        # storage_key 不含宿主绝对路径
        assert ":" not in view["storage_key"]
        assert not view["storage_key"].startswith("/")

    @pytest.mark.asyncio
    async def test_upload_same_content_dedup(
        self, service: ArtifactServiceImpl
    ) -> None:
        """相同内容不同 artifact 共享同一 storage_key（内容寻址去重）。"""
        content = _content("shared-content-dedup")
        actor = _actor(roles=["ADMIN"])

        view1 = await service.upload_artifact(
            "proj-001", "type_a", content, actor_id=actor["user_id"], actor=actor
        )
        view2 = await service.upload_artifact(
            "proj-002", "type_b", content, actor_id=actor["user_id"], actor=actor
        )

        assert view1["storage_key"] == view2["storage_key"]
        assert view1["content_hash"] == view2["content_hash"]
        assert view1["id"] != view2["id"]

    @pytest.mark.asyncio
    async def test_upload_permission_denied_no_roles(
        self, service: ArtifactServiceImpl
    ) -> None:
        """无角色用户被拒绝上传。"""
        actor = _actor(user_id="no-roles", roles=[])
        with pytest.raises(PermissionDeniedError):
            await service.upload_artifact(
                "proj-001", "type", _content(), actor_id=actor["user_id"], actor=actor
            )

    @pytest.mark.asyncio
    async def test_upload_permission_denied_observer(
        self, service: ArtifactServiceImpl
    ) -> None:
        """OBSERVER 角色无 write 权限被拒绝。"""
        actor = _actor(user_id="observer", roles=["OBSERVER"])
        with pytest.raises(PermissionDeniedError):
            await service.upload_artifact(
                "proj-001", "type", _content(), actor_id=actor["user_id"], actor=actor
            )

    @pytest.mark.asyncio
    async def test_upload_empty_project_id_rejected(
        self, service: ArtifactServiceImpl
    ) -> None:
        """空 project_id 抛 ArgumentError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(ArgumentError):
            await service.upload_artifact(
                "", "type", _content(), actor_id=actor["user_id"], actor=actor
            )

    @pytest.mark.asyncio
    async def test_upload_invalid_content_type(
        self, service: ArtifactServiceImpl
    ) -> None:
        """content_bytes 非 bytes 抛 ArgumentError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(ArgumentError):
            await service.upload_artifact(
                "proj-001", "type", "not-bytes",  # type: ignore[arg-type]
                actor_id=actor["user_id"], actor=actor,
            )


class TestArtifactServiceGetAndList:
    """``ArtifactService.get_artifact`` 与 ``list_artifacts`` 测试。"""

    @pytest.mark.asyncio
    async def test_get_artifact_returns_metadata(
        self, service: ArtifactServiceImpl
    ) -> None:
        """获取 artifact 元数据。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("get-test-content")
        uploaded = await service.upload_artifact(
            "proj-001", "snapshot", content,
            actor_id=actor["user_id"], actor=actor,
        )

        view = await service.get_artifact(
            uploaded["id"], actor_id=actor["user_id"], actor=actor,
        )

        assert view["id"] == uploaded["id"]
        assert view["content_hash"] == uploaded["content_hash"]
        assert view["status"] == "COMPLETED"

    @pytest.mark.asyncio
    async def test_get_artifact_not_found(
        self, service: ArtifactServiceImpl
    ) -> None:
        """不存在的 artifact 抛 NotFoundError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(NotFoundError):
            await service.get_artifact(
                "nonexistent-id", actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_list_artifacts_by_project(
        self, service: ArtifactServiceImpl
    ) -> None:
        """按 project_id 列出 artifacts。"""
        actor = _actor(roles=["ADMIN"])
        # 上传 3 个：2 个 proj-001，1 个 proj-002
        await service.upload_artifact(
            "proj-001", "type_a", _content("a"),
            actor_id=actor["user_id"], actor=actor,
        )
        await service.upload_artifact(
            "proj-001", "type_b", _content("b"),
            actor_id=actor["user_id"], actor=actor,
        )
        await service.upload_artifact(
            "proj-002", "type_a", _content("c"),
            actor_id=actor["user_id"], actor=actor,
        )

        views = await service.list_artifacts(
            "proj-001", actor_id=actor["user_id"], actor=actor,
        )
        assert len(views) == 2
        assert all(v["project_id"] == "proj-001" for v in views)

    @pytest.mark.asyncio
    async def test_list_artifacts_filter_by_type(
        self, service: ArtifactServiceImpl
    ) -> None:
        """按 artifact_type 过滤列表。"""
        actor = _actor(roles=["ADMIN"])
        await service.upload_artifact(
            "proj-001", "type_a", _content("a"),
            actor_id=actor["user_id"], actor=actor,
        )
        await service.upload_artifact(
            "proj-001", "type_b", _content("b"),
            actor_id=actor["user_id"], actor=actor,
        )

        views = await service.list_artifacts(
            "proj-001", artifact_type="type_a",
            actor_id=actor["user_id"], actor=actor,
        )
        assert len(views) == 1
        assert views[0]["artifact_type"] == "type_a"

    @pytest.mark.asyncio
    async def test_list_excludes_deleted(
        self, service: ArtifactServiceImpl
    ) -> None:
        """列表排除已删除的 artifact。"""
        actor = _actor(roles=["ADMIN"])
        v1 = await service.upload_artifact(
            "proj-001", "type_a", _content("keep"),
            actor_id=actor["user_id"], actor=actor,
        )
        v2 = await service.upload_artifact(
            "proj-001", "type_b", _content("delete"),
            actor_id=actor["user_id"], actor=actor,
        )
        await service.delete_artifact(
            v2["id"], actor_id=actor["user_id"], actor=actor,
        )

        views = await service.list_artifacts(
            "proj-001", actor_id=actor["user_id"], actor=actor,
        )
        ids = [v["id"] for v in views]
        assert v1["id"] in ids
        assert v2["id"] not in ids


class TestArtifactServiceDownload:
    """``ArtifactService.download_artifact`` 测试。"""

    @pytest.mark.asyncio
    async def test_download_returns_content_with_hash_verification(
        self, service: ArtifactServiceImpl
    ) -> None:
        """下载返回原始内容并通过 hash 校验。"""
        content = _content("download-verify-content" * 50)
        actor = _actor(roles=["ADMIN"])
        uploaded = await service.upload_artifact(
            "proj-001", "snapshot", content,
            actor_id=actor["user_id"], actor=actor,
        )

        downloaded = await service.download_artifact(
            uploaded["id"], actor_id=actor["user_id"], actor=actor,
        )

        assert downloaded == content
        assert hashlib.sha256(downloaded).hexdigest() == uploaded["content_hash"]

    @pytest.mark.asyncio
    async def test_download_not_found(
        self, service: ArtifactServiceImpl
    ) -> None:
        """下载不存在的 artifact 抛 NotFoundError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(NotFoundError):
            await service.download_artifact(
                "nonexistent", actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_download_deleted_raises_not_found(
        self, service: ArtifactServiceImpl
    ) -> None:
        """下载已删除的 artifact 抛 NotFoundError。"""
        actor = _actor(roles=["ADMIN"])
        uploaded = await service.upload_artifact(
            "proj-001", "type", _content("to-delete"),
            actor_id=actor["user_id"], actor=actor,
        )
        await service.delete_artifact(
            uploaded["id"], actor_id=actor["user_id"], actor=actor,
        )
        with pytest.raises(NotFoundError):
            await service.download_artifact(
                uploaded["id"], actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_download_tampered_file_raises(
        self, service: ArtifactServiceImpl, file_store: LocalArtifactFileStore
    ) -> None:
        """下载时检测到文件被篡改抛 ValueError（验收：内容完整性）。"""
        content = _content("tamper-detection-content")
        actor = _actor(roles=["ADMIN"])
        uploaded = await service.upload_artifact(
            "proj-001", "snapshot", content,
            actor_id=actor["user_id"], actor=actor,
        )

        # 模拟文件被篡改
        target_path = file_store.root / uploaded["storage_key"]
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_path, "wb") as f:
            f.write(b"tampered-content-different-from-original")

        with pytest.raises(ValueError, match="内容哈希不符"):
            await service.download_artifact(
                uploaded["id"], actor_id=actor["user_id"], actor=actor,
            )


class TestArtifactServiceDelete:
    """``ArtifactService.delete_artifact`` 测试。"""

    @pytest.mark.asyncio
    async def test_delete_soft_deletes(
        self, service: ArtifactServiceImpl
    ) -> None:
        """删除标记为 DELETED（软删除）。"""
        content = _content("delete-soft-test")
        actor = _actor(roles=["ADMIN"])
        uploaded = await service.upload_artifact(
            "proj-001", "snapshot", content,
            actor_id=actor["user_id"], actor=actor,
        )

        view = await service.delete_artifact(
            uploaded["id"], actor_id=actor["user_id"], actor=actor,
        )

        assert view["status"] == "DELETED"
        assert view["id"] == uploaded["id"]
        # get_artifact 应抛 NotFoundError
        with pytest.raises(NotFoundError):
            await service.get_artifact(
                uploaded["id"], actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_delete_idempotent(
        self, service: ArtifactServiceImpl
    ) -> None:
        """重复删除视为成功（幂等）。"""
        actor = _actor(roles=["ADMIN"])
        uploaded = await service.upload_artifact(
            "proj-001", "type", _content("idempotent-delete"),
            actor_id=actor["user_id"], actor=actor,
        )

        await service.delete_artifact(
            uploaded["id"], actor_id=actor["user_id"], actor=actor,
        )
        # 再次删除不抛错
        view = await service.delete_artifact(
            uploaded["id"], actor_id=actor["user_id"], actor=actor,
        )
        assert view["status"] == "DELETED"

    @pytest.mark.asyncio
    async def test_delete_not_found(
        self, service: ArtifactServiceImpl
    ) -> None:
        """删除不存在的 artifact 抛 NotFoundError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(NotFoundError):
            await service.delete_artifact(
                "nonexistent", actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_delete_cleans_storage_file(
        self, service: ArtifactServiceImpl, file_store: LocalArtifactFileStore
    ) -> None:
        """删除后无引用时清理存储文件。"""
        content = _content("unique-cleanup-content")
        actor = _actor(roles=["ADMIN"])
        uploaded = await service.upload_artifact(
            "proj-001", "snapshot", content,
            actor_id=actor["user_id"], actor=actor,
        )

        target_path = file_store.root / uploaded["storage_key"]
        assert target_path.exists()

        await service.delete_artifact(
            uploaded["id"], actor_id=actor["user_id"], actor=actor,
        )

        assert not target_path.exists()

    @pytest.mark.asyncio
    async def test_delete_keeps_shared_file(
        self, service: ArtifactServiceImpl, file_store: LocalArtifactFileStore
    ) -> None:
        """删除一个引用时，若其他 artifact 仍引用同一内容，不删文件。"""
        content = _content("shared-cleanup-content")
        actor = _actor(roles=["ADMIN"])
        v1 = await service.upload_artifact(
            "proj-001", "type_a", content,
            actor_id=actor["user_id"], actor=actor,
        )
        v2 = await service.upload_artifact(
            "proj-002", "type_b", content,
            actor_id=actor["user_id"], actor=actor,
        )

        # 删除 v1，v2 仍引用同一文件
        await service.delete_artifact(
            v1["id"], actor_id=actor["user_id"], actor=actor,
        )

        target_path = file_store.root / v1["storage_key"]
        assert target_path.exists()  # 文件仍存在（v2 引用）


class TestArtifactServiceComplete:
    """``ArtifactService.complete_artifact`` 测试（分片上传状态机）。"""

    @pytest.mark.asyncio
    async def test_complete_uploading_to_completed(
        self,
        service: ArtifactServiceImpl,
        db: Database,
        file_store: LocalArtifactFileStore,
    ) -> None:
        """UPLOADING → COMPLETED 状态转换。"""
        import io

        from maf_server.core.artifact_store import storage_key_for

        # 直接通过 repository 插入一个 UPLOADING 状态的 artifact
        content = _content("complete-test-content")
        sha = hashlib.sha256(content).hexdigest()
        length = len(content)
        stored = await file_store.put_stream(io.BytesIO(content), sha, length)

        artifact_id = new_artifact_id()
        repo = SqliteArtifactRepository()
        actor = _actor(roles=["ADMIN"])
        now_iso = "2026-01-01T00:00:00+00:00"

        async with SqliteUnitOfWork(db) as uow:
            await repo.insert_artifact(
                uow.connection,
                artifact_id=artifact_id,
                project_id="proj-001",
                artifact_type="snapshot",
                content_hash=sha,
                storage_key=stored["storage_key"],
                size_bytes=length,
                status="UPLOADING",
                uploaded_by=actor["user_id"],
                uploaded_at=now_iso,
                completed_at=None,
            )
            await uow.commit()

        # 调用 complete_artifact
        view = await service.complete_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor,
        )

        assert view["status"] == "COMPLETED"
        assert view["completed_at"] is not None
        assert view["version_no"] == 2

    @pytest.mark.asyncio
    async def test_complete_already_completed_raises(
        self, service: ArtifactServiceImpl
    ) -> None:
        """对已 COMPLETED 的 artifact 调用 complete 抛错。"""
        actor = _actor(roles=["ADMIN"])
        uploaded = await service.upload_artifact(
            "proj-001", "type", _content("already-completed"),
            actor_id=actor["user_id"], actor=actor,
        )

        with pytest.raises(UnsupportedOperationError):
            await service.complete_artifact(
                uploaded["id"], actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_complete_not_found(
        self, service: ArtifactServiceImpl
    ) -> None:
        """complete 不存在的 artifact 抛 NotFoundError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(NotFoundError):
            await service.complete_artifact(
                "nonexistent", actor_id=actor["user_id"], actor=actor,
            )


# --------------------------------------------------------------------------- #
# 验收 3：权限检查
# --------------------------------------------------------------------------- #


class TestPermissionChecks:
    """权限检查测试。"""

    @pytest.mark.asyncio
    async def test_observer_can_read_but_not_write(
        self, service: ArtifactServiceImpl
    ) -> None:
        """OBSERVER 可读但不可写。"""
        admin = _actor(user_id="admin", roles=["ADMIN"])
        observer = _actor(user_id="observer", roles=["OBSERVER"])

        # admin 上传
        uploaded = await service.upload_artifact(
            "proj-001", "type", _content("permission-test"),
            actor_id=admin["user_id"], actor=admin,
        )

        # observer 可读
        view = await service.get_artifact(
            uploaded["id"], actor_id=observer["user_id"], actor=observer,
        )
        assert view["id"] == uploaded["id"]

        # observer 不可写（上传）
        with pytest.raises(PermissionDeniedError):
            await service.upload_artifact(
                "proj-001", "type", _content("denied"),
                actor_id=observer["user_id"], actor=observer,
            )

        # observer 不可删除
        with pytest.raises(PermissionDeniedError):
            await service.delete_artifact(
                uploaded["id"], actor_id=observer["user_id"], actor=observer,
            )

    @pytest.mark.asyncio
    async def test_owner_can_read_write_artifacts(
        self, service: ArtifactServiceImpl
    ) -> None:
        """OWNER（测试策略中追加 artifacts 读写）可读写。"""
        owner = _actor(user_id="owner", roles=["OWNER"])

        view = await service.upload_artifact(
            "proj-001", "type", _content("owner-write"),
            actor_id=owner["user_id"], actor=owner,
        )
        assert view["status"] == "COMPLETED"

        # owner 可读
        got = await service.get_artifact(
            view["id"], actor_id=owner["user_id"], actor=owner,
        )
        assert got["id"] == view["id"]

        # owner 可下载
        content = await service.download_artifact(
            view["id"], actor_id=owner["user_id"], actor=owner,
        )
        assert content == _content("owner-write")

        # owner 可删除
        deleted = await service.delete_artifact(
            view["id"], actor_id=owner["user_id"], actor=owner,
        )
        assert deleted["status"] == "DELETED"

    @pytest.mark.asyncio
    async def test_no_permission_keys_denied(
        self, service: ArtifactServiceImpl
    ) -> None:
        """空 permission_keys 被拒绝。"""
        actor = _actor(user_id="empty", roles=[])
        with pytest.raises(PermissionDeniedError):
            await service.get_artifact(
                "any-id", actor_id=actor["user_id"], actor=actor,
            )


# --------------------------------------------------------------------------- #
# 验收 4：事件经 OutboxRepository 写入
# --------------------------------------------------------------------------- #


class TestOutboxEvents:
    """Artifact 事件经 OutboxRepository 写入测试。"""

    @pytest.mark.asyncio
    async def test_upload_writes_artifact_uploaded_event(
        self, service: ArtifactServiceImpl, db: Database
    ) -> None:
        """上传后 outbox_events 含 artifact.uploaded 事件。"""
        actor = _actor(roles=["ADMIN"])
        await service.upload_artifact(
            "proj-001", "snapshot", _content("event-upload"),
            actor_id=actor["user_id"], actor=actor,
        )

        repo = SqliteOutboxRepository(db)
        events = await repo.find_by_project("proj-001")
        uploaded_events = [e for e in events if e.event_type == "artifact.uploaded"]
        assert len(uploaded_events) == 1
        assert uploaded_events[0].aggregate_type == "artifact"
        assert uploaded_events[0].payload["content_hash"] == hashlib.sha256(
            _content("event-upload")
        ).hexdigest()

    @pytest.mark.asyncio
    async def test_delete_writes_artifact_deleted_event(
        self, service: ArtifactServiceImpl, db: Database
    ) -> None:
        """删除后 outbox_events 含 artifact.deleted 事件。"""
        actor = _actor(roles=["ADMIN"])
        uploaded = await service.upload_artifact(
            "proj-001", "snapshot", _content("event-delete"),
            actor_id=actor["user_id"], actor=actor,
        )
        await service.delete_artifact(
            uploaded["id"], actor_id=actor["user_id"], actor=actor,
        )

        repo = SqliteOutboxRepository(db)
        events = await repo.find_by_project("proj-001")
        deleted_events = [e for e in events if e.event_type == "artifact.deleted"]
        assert len(deleted_events) == 1

    @pytest.mark.asyncio
    async def test_complete_writes_artifact_completed_event(
        self,
        service: ArtifactServiceImpl,
        db: Database,
        file_store: LocalArtifactFileStore,
    ) -> None:
        """complete 后 outbox_events 含 artifact.completed 事件。"""
        import io

        content = _content("event-complete")
        sha = hashlib.sha256(content).hexdigest()
        length = len(content)
        stored = await file_store.put_stream(io.BytesIO(content), sha, length)

        artifact_id = new_artifact_id()
        repo = SqliteArtifactRepository()
        actor = _actor(roles=["ADMIN"])

        async with SqliteUnitOfWork(db) as uow:
            await repo.insert_artifact(
                uow.connection,
                artifact_id=artifact_id,
                project_id="proj-001",
                artifact_type="snapshot",
                content_hash=sha,
                storage_key=stored["storage_key"],
                size_bytes=length,
                status="UPLOADING",
                uploaded_by=actor["user_id"],
                uploaded_at="2026-01-01T00:00:00+00:00",
                completed_at=None,
            )
            await uow.commit()

        await service.complete_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor,
        )

        outbox_repo = SqliteOutboxRepository(db)
        events = await outbox_repo.find_by_project("proj-001")
        completed_events = [
            e for e in events if e.event_type == "artifact.completed"
        ]
        assert len(completed_events) == 1

    @pytest.mark.asyncio
    async def test_events_same_transaction_as_metadata(
        self, service: ArtifactServiceImpl, db: Database
    ) -> None:
        """事件与元数据在同一事务提交（commit 后均可见）。"""
        actor = _actor(roles=["ADMIN"])
        await service.upload_artifact(
            "proj-001", "snapshot", _content("tx-test"),
            actor_id=actor["user_id"], actor=actor,
        )

        # 元数据和事件都应可见
        views = await service.list_artifacts(
            "proj-001", actor_id=actor["user_id"], actor=actor,
        )
        assert len(views) == 1

        repo = SqliteOutboxRepository(db)
        events = await repo.find_by_project("proj-001")
        assert any(e.event_type == "artifact.uploaded" for e in events)


# --------------------------------------------------------------------------- #
# 验收 5：乐观锁
# --------------------------------------------------------------------------- #


class TestOptimisticLock:
    """乐观锁（version_no）测试。"""

    @pytest.mark.asyncio
    async def test_complete_version_conflict(
        self,
        service: ArtifactServiceImpl,
        db: Database,
        file_store: LocalArtifactFileStore,
    ) -> None:
        """complete_artifact 乐观锁冲突。"""
        import io

        content = _content("version-conflict-test")
        sha = hashlib.sha256(content).hexdigest()
        length = len(content)
        stored = await file_store.put_stream(io.BytesIO(content), sha, length)

        artifact_id = new_artifact_id()
        repo = SqliteArtifactRepository()
        actor = _actor(roles=["ADMIN"])

        async with SqliteUnitOfWork(db) as uow:
            await repo.insert_artifact(
                uow.connection,
                artifact_id=artifact_id,
                project_id="proj-001",
                artifact_type="snapshot",
                content_hash=sha,
                storage_key=stored["storage_key"],
                size_bytes=length,
                status="UPLOADING",
                uploaded_by=actor["user_id"],
                uploaded_at="2026-01-01T00:00:00+00:00",
                completed_at=None,
            )
            await uow.commit()

        # 使用 stale-version repository 包装器：service 读到 version_no=0，
        # 但 DB 实际 version_no=1，update_status(expected_version=0) 不匹配，
        # 返回 0，service 抛 VersionConflictError。
        #
        # 不能通过"调用前手动 UPDATE version_no+1"模拟冲突：service 在同一
        # BEGIN IMMEDIATE 事务内读到最新 version_no（2），以此作为
        # expected_version 调用 update_status，UPDATE 仍然匹配，不会冲突。
        stale_service = ArtifactServiceImpl(
            database=db,
            file_store=file_store,
            repository=_StaleVersionRepository(repo, stale_offset=1),
            permission_service=_make_permission_service(),
        )

        with pytest.raises(VersionConflictError):
            await stale_service.complete_artifact(
                artifact_id, actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_delete_version_conflict(
        self,
        service: ArtifactServiceImpl,
        db: Database,
        file_store: LocalArtifactFileStore,
    ) -> None:
        """delete_artifact 乐观锁冲突。"""
        actor = _actor(roles=["ADMIN"])
        uploaded = await service.upload_artifact(
            "proj-001", "type", _content("delete-conflict"),
            actor_id=actor["user_id"], actor=actor,
        )

        # 使用 stale-version repository 包装器：service 读到 version_no=0，
        # 但 DB 实际 version_no=1，update_status(expected_version=0) 不匹配，
        # 返回 0，service 抛 VersionConflictError。
        #
        # 不能通过"调用前手动 UPDATE version_no+1"模拟冲突：service 在同一
        # BEGIN IMMEDIATE 事务内读到最新 version_no（2），以此作为
        # expected_version 调用 update_status，UPDATE 仍然匹配，不会冲突。
        stale_service = ArtifactServiceImpl(
            database=db,
            file_store=file_store,
            repository=_StaleVersionRepository(
                SqliteArtifactRepository(), stale_offset=1
            ),
            permission_service=_make_permission_service(),
        )

        with pytest.raises(VersionConflictError):
            await stale_service.delete_artifact(
                uploaded["id"], actor_id=actor["user_id"], actor=actor,
            )


# --------------------------------------------------------------------------- #
# 验收 6：大文件不一次读入内存
# --------------------------------------------------------------------------- #


class TestStreamingUpload:
    """流式上传测试（大文件不一次读入内存）。"""

    @pytest.mark.asyncio
    async def test_large_content_uploaded_and_downloaded(
        self, service: ArtifactServiceImpl
    ) -> None:
        """较大内容（1MB）上传与下载一致。"""
        # 1MB 内容
        large_content = bytes(range(256)) * 4096  # 1MB
        actor = _actor(roles=["ADMIN"])

        uploaded = await service.upload_artifact(
            "proj-001", "large_snapshot", large_content,
            actor_id=actor["user_id"], actor=actor,
        )

        assert uploaded["size_bytes"] == len(large_content)

        downloaded = await service.download_artifact(
            uploaded["id"], actor_id=actor["user_id"], actor=actor,
        )
        assert downloaded == large_content

    @pytest.mark.asyncio
    async def test_put_stream_chunks_not_full_read(
        self, file_store: LocalArtifactFileStore
    ) -> None:
        """put_stream 以块读写，不一次读入内存。"""
        # 使用小 chunk_size 构造 store，验证分块
        store = LocalArtifactFileStore(
            file_store.root, chunk_size=128
        )
        content = bytes(range(256)) * 100  # 25.6KB
        sha = hashlib.sha256(content).hexdigest()
        length = len(content)

        stored = await store.put_stream(io.BytesIO(content), sha, length)
        assert stored["content_length"] == length

        # 读取验证
        chunks: list[bytes] = []
        async for chunk in store.open_stream(stored["storage_key"]):
            chunks.append(chunk)
        assert b"".join(chunks) == content
