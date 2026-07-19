"""TASK-035 集成测试：仓库绑定与验证。

验收标准覆盖：
1. **GitHub 与本地 Git 都返回统一健康结果**：``GitRepositoryAdapter.verify`` 对本地
   Git 仓库返回 ``VerifyResult``，``repository_info`` 含 ``default_branch``、
   ``branches``、``can_read``、``can_write``。GitHub 走同一适配器接口，结构统一。
2. **凭据不复制进绑定表**：HTTPS token 明文只存于 SecretService，绑定表仅保留
   ``credential_secret_id``；SSH key 路径存入 ``ssh_key_path``（路径不是密钥），
   密钥内容绝不入库。明文不出现在事件 payload。
3. **验证只做安全探测，不修改主分支**：``verify`` 使用 ``clone --bare``（只读探测）、
   ``for-each-ref``（列分支）、``rev-parse``（默认分支）、``push --dry-run``（探测
   写权限）。验证后源仓库 HEAD commit 与分支列表不变。

测试策略：
- 使用 ``init_local_git_repo`` 创建真实本地 Git 仓库（不依赖 GitHub）。
- ``GitRepositoryAdapter`` 对本地 ``file://`` 仓库执行真实 clone/for-each-ref/
  rev-parse/push --dry-run，验证安全探测语义。
- ``RepositoryBindingServiceImpl`` 完整测试 bind/verify/list/remove 与权限/事件/
  凭据安全。
- 凭据安全：验证 HTTPS token 明文不进入数据库、事件或日志。

测试范围禁止：不测试 PR 创建与合并（属 TASK-083+ 范围）。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from maf_domain.errors import (
    ArgumentError,
    NotFoundError,
    PermissionDeniedError,
)
from maf_server.config import ServerSettings
from maf_server.core.database import Database
from maf_server.core.events import init_outbox_schema
from maf_server.core.secrets import MASTER_KEY_SIZE_BYTES
from maf_server.gateway.repository.adapter import GitRepositoryAdapter
from maf_server.gateway.repository.service import VerifyResult
from maf_server.gateway.secrets.aes_gcm_store import AesGcmFileStore
from maf_server.gateway.secrets.local_service import LocalSecretService
from maf_server.modules.iam.repository import (
    SqliteIamRepository,
    init_schema as init_iam_schema,
)
from maf_server.modules.iam.service import seed_local_user
from maf_server.modules.projects.repository import SqliteProjectRepository
from maf_server.modules.projects.service import ProjectApplicationServiceImpl
from maf_server.modules.repositories.repository import (
    SqliteRepositoryBindingRepository,
    init_schema as init_repositories_schema,
)
from maf_server.modules.repositories.service import RepositoryBindingServiceImpl

from tests.fixtures.git_repo import LocalGitRepo, init_local_git_repo

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

_SECRET_TOKEN = "ghp_TEST_TOKEN_task035_secret_xyz_12345"
_SSH_KEY_CONTENT = "FAKE-SSH-PRIVATE-KEY-MATERIAL-for-task-035"
_ORG_ID = "org-001"
_TEST_PASSWORD = "repo-binding-correct-horse-battery-staple"

#: projects 与 project_members 建表 SQL（与 test_projects.py 保持一致）。
_PROJECTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id              TEXT    PRIMARY KEY,
    name            TEXT    NOT NULL,
    description     TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'ACTIVE',
    created_at      TEXT    NOT NULL,
    created_by      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    version_no      INTEGER NOT NULL DEFAULT 1,
    deleted_at      TEXT,
    CHECK (status IN ('ACTIVE', 'ARCHIVED'))
);

CREATE INDEX IF NOT EXISTS idx_projects_created_by ON projects(created_by);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);

CREATE TABLE IF NOT EXISTS project_members (
    project_id      TEXT    NOT NULL,
    user_id         TEXT    NOT NULL,
    role            TEXT    NOT NULL,
    added_at        TEXT    NOT NULL,
    added_by        TEXT    NOT NULL,
    version_no      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (project_id, user_id),
    CHECK (role IN ('OWNER', 'APPROVER', 'OBSERVER', 'DESIGNER'))
);

CREATE INDEX IF NOT EXISTS idx_project_members_user_id ON project_members(user_id);
CREATE INDEX IF NOT EXISTS idx_project_members_role ON project_members(project_id, role);
"""


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #


def _token_bytes() -> bytes:
    """生成 AES master key。"""
    import secrets as _s

    return _s.token_bytes(MASTER_KEY_SIZE_BYTES)


def _make_settings(tmp_path: Path, **overrides: object) -> ServerSettings:
    """构造测试用 ``ServerSettings``。"""
    kwargs: dict[str, object] = dict(
        organization_id=_ORG_ID,
        business_db_path=Path("maf.db"),
        checkpointer_db_path=Path("checkpoints.db"),
        artifact_root=Path("artifacts"),
        workspace_root=Path("workspaces"),
        git_repo_root=tmp_path / "repo",
        public_base_url="http://localhost:8000",
        secret_key="test-secret-for-task-035",
        data_dir=tmp_path,
        _env_file=None,
    )
    kwargs.update(overrides)
    return ServerSettings(**kwargs)


async def _init_projects_schema(database: Database) -> None:
    """创建 projects 与 project_members 表。"""
    async with database.write_connection() as conn:
        for stmt in _PROJECTS_SCHEMA_SQL.split(";"):
            stripped = stmt.strip()
            if stripped:
                await conn.execute(stripped)


def _repo_url(path: Path) -> str:
    """把本地路径转为 ``file://`` URL，供 git clone 使用。"""
    return path.resolve().as_uri()


def _add_branch(repo: LocalGitRepo, branch_name: str) -> None:
    """在本地仓库创建新分支并切换回 main。"""
    repo.checkout_branch(branch_name, create=True)
    repo.commit_file(
        f"file_on_{branch_name}.txt",
        f"content on {branch_name}",
        f"add file on {branch_name}",
    )
    repo.checkout_branch("main")


async def _list_outbox_events(
    db: Database, project_id: str | None = None
) -> list[dict]:
    """列出 outbox_events 事件（按 occurred_at 升序）。"""
    sql = (
        "SELECT event_type, aggregate_type, aggregate_id, project_id, payload "
        "FROM outbox_events "
    )
    params: tuple = ()
    if project_id is not None:
        sql += "WHERE project_id = ? "
        params = (project_id,)
    sql += "ORDER BY occurred_at ASC, id ASC"
    async with db.read_connection() as conn:
        async with conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
    return [
        {
            "event_type": r[0],
            "aggregate_type": r[1],
            "aggregate_id": r[2],
            "project_id": r[3],
            "payload": json.loads(r[4]),
        }
        for r in rows
    ]


async def _read_binding_row(db: Database, binding_id: str) -> dict:
    """读取 ``project_repositories`` 原始行（含 credential_secret_id/ssh_key_path）。"""
    async with db.read_connection() as conn:
        async with conn.execute(
            "SELECT id, project_id, repository_url, branch, credential_type, "
            "credential_secret_id, ssh_key_path, verified, verified_at, "
            "bound_by, bound_at, version_no "
            "FROM project_repositories WHERE id = ?",
            (binding_id,),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return {}
    return {
        "id": row[0],
        "project_id": row[1],
        "repository_url": row[2],
        "branch": row[3],
        "credential_type": row[4],
        "credential_secret_id": row[5],
        "ssh_key_path": row[6],
        "verified": bool(row[7]),
        "verified_at": row[8],
        "bound_by": row[9],
        "bound_at": row[10],
        "version_no": row[11],
    }


async def _list_remote_branches(repo: LocalGitRepo) -> list[str]:
    """列出本地仓库的所有分支名（不含 refs/heads/ 前缀）。"""
    out = repo.run(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads/"]
    )
    return sorted(line.strip() for line in out.splitlines() if line.strip())


async def _create_project_with_members(
    db: Database, admin_id: str, observer_id: str | None = None
) -> str:
    """创建项目（admin 为 OWNER），可选添加 observer 为 OBSERVER 成员。"""
    project_service = ProjectApplicationServiceImpl(
        db,
        organization_id=_ORG_ID,
        iam_repository=SqliteIamRepository(),
        project_repository=SqliteProjectRepository(),
    )
    project = await project_service.create_project(
        "Repo Binding Test", "", actor_id=admin_id
    )
    if observer_id is not None:
        await project_service.add_member(
            project["id"], observer_id, "OBSERVER", actor_id=admin_id
        )
    return project["id"]


def _make_service(
    db: Database,
    adapter: GitRepositoryAdapter,
    secret_service: LocalSecretService | None = None,
) -> RepositoryBindingServiceImpl:
    """构造 ``RepositoryBindingServiceImpl`` 实例。"""
    return RepositoryBindingServiceImpl(
        db,
        adapter=adapter,
        secret_service=secret_service,
        organization_id=_ORG_ID,
    )


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_maf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除所有 ``MAF_*`` 环境变量，保证测试从干净状态开始。"""
    for key in list(os.environ):
        if key.startswith("MAF_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def source_repo(tmp_path: Path) -> LocalGitRepo:
    """创建真实本地 Git 仓库（含 main 分支和初始提交）。"""
    return init_local_git_repo(tmp_path / "source_repo")


@pytest.fixture()
def multi_branch_repo(tmp_path: Path) -> LocalGitRepo:
    """创建含 main + feature/test 分支的本地 Git 仓库。"""
    repo = init_local_git_repo(tmp_path / "multi_branch_repo")
    _add_branch(repo, "feature/test")
    return repo


@pytest.fixture()
def aes_store(tmp_path: Path) -> AesGcmFileStore:
    """AES-GCM 文件密钥库。"""
    return AesGcmFileStore(
        master_key=_token_bytes(),
        storage_dir=tmp_path / "secrets",
        organization_id=_ORG_ID,
    )


@pytest.fixture()
def secret_service(aes_store: AesGcmFileStore) -> LocalSecretService:
    """本地 SecretService 实例。"""
    return LocalSecretService(primary=aes_store)


@pytest.fixture()
def ssh_key_file(tmp_path: Path) -> Path:
    """临时 SSH 私钥文件（绝对路径，内容为 fake material）。"""
    key_path = tmp_path / "ssh" / "id_ed25519"
    key_path.parent.mkdir(parents=True)
    key_path.write_text(_SSH_KEY_CONTENT, encoding="utf-8")
    return key_path


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    """已初始化 IAM + projects + project_repositories + outbox schema 的 Database。"""
    settings = _make_settings(tmp_path)
    database = Database(settings)
    await database.initialize()
    async with database.write_connection() as conn:
        await init_iam_schema(conn)
    await _init_projects_schema(database)
    async with database.write_connection() as conn:
        await init_repositories_schema(conn)
    await init_outbox_schema(database)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def admin_db(db: Database) -> tuple[Database, str]:
    """已种子 ADMIN 用户的 Database；返回 (db, admin_user_id)。"""
    admin_id = await seed_local_user(
        db,
        username="admin",
        display_name="Admin User",
        password_plain=_TEST_PASSWORD,
        permission_keys=["ADMIN"],
    )
    return db, admin_id


@pytest_asyncio.fixture
async def observer_db(
    admin_db: tuple[Database, str]
) -> tuple[Database, str, str]:
    """已种子 ADMIN + OBSERVER 用户的 Database；返回 (db, admin_id, observer_id)。"""
    db, admin_id = admin_db
    observer_id = await seed_local_user(
        db,
        username="observer",
        display_name="Observer User",
        password_plain=_TEST_PASSWORD,
        permission_keys=["OBSERVER"],
    )
    return db, admin_id, observer_id


@pytest_asyncio.fixture
async def owner_nonmember_db(
    observer_db: tuple[Database, str, str]
) -> tuple[Database, str, str, str]:
    """已种子 ADMIN + OBSERVER + OWNER(非成员) 用户。

    返回 (db, admin_id, observer_id, owner_nonmember_id)。
    owner_nonmember 有全局 OWNER 权限但不是任何项目的成员。
    """
    db, admin_id, observer_id = observer_db
    owner_nm_id = await seed_local_user(
        db,
        username="owner-nm",
        display_name="Owner NonMember",
        password_plain=_TEST_PASSWORD,
        permission_keys=["OWNER"],
    )
    return db, admin_id, observer_id, owner_nm_id


@pytest_asyncio.fixture
async def adapter(tmp_path: Path) -> GitRepositoryAdapter:
    """``GitRepositoryAdapter`` 实例，workspace 在 tmp_path 下。"""
    workspace = tmp_path / "adapter_workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return GitRepositoryAdapter(workspace_root=workspace)


# --------------------------------------------------------------------------- #
# 验收 1：GitHub 与本地 Git 都返回统一健康结果
# --------------------------------------------------------------------------- #


class TestUnifiedHealthResult:
    """``GitRepositoryAdapter.verify`` 返回统一结构的 ``VerifyResult``。"""

    @pytest.mark.asyncio
    async def test_local_git_returns_unified_verify_result(
        self, adapter: GitRepositoryAdapter, source_repo: LocalGitRepo
    ) -> None:
        """本地 Git verify 返回 VerifyResult，repository_info 含统一字段。"""
        result = await adapter.verify(
            _repo_url(source_repo.path),
            {"type": "NONE"},
            expected_branch="main",
        )

        assert isinstance(result, VerifyResult)
        assert result.verified is True
        assert result.error is None
        # repository_info 结构与 RepositoryInfo TypedDict 一致
        info = result.repository_info
        assert info is not None
        assert set(info.keys()) == {
            "default_branch",
            "branches",
            "can_read",
            "can_write",
        }
        assert info["default_branch"] == "main"
        assert info["branches"] == ["main"]
        assert info["can_read"] is True
        assert info["can_write"] is True

    @pytest.mark.asyncio
    async def test_unified_result_for_multi_branch_repo(
        self, adapter: GitRepositoryAdapter, multi_branch_repo: LocalGitRepo
    ) -> None:
        """多分支仓库返回统一结构，branches 包含所有分支。"""
        result = await adapter.verify(
            _repo_url(multi_branch_repo.path),
            {"type": "NONE"},
            expected_branch="main",
        )

        assert result.verified is True
        info = result.repository_info
        assert info is not None
        assert info["default_branch"] == "main"
        assert "main" in info["branches"]
        assert "feature/test" in info["branches"]
        assert info["can_read"] is True

    @pytest.mark.asyncio
    async def test_verify_result_structure_consistent_on_failure(
        self, adapter: GitRepositoryAdapter
    ) -> None:
        """验证失败时 VerifyResult 结构一致（verified=False, repository_info=None）。"""
        result = await adapter.verify(
            "file:///nonexistent/path/to/repo",
            {"type": "NONE"},
            expected_branch="main",
        )

        assert isinstance(result, VerifyResult)
        assert result.verified is False
        assert result.repository_info is None
        assert result.error is not None
        assert "clone failed" in result.error

    @pytest.mark.asyncio
    async def test_list_branches_returns_unified_list(
        self, adapter: GitRepositoryAdapter, multi_branch_repo: LocalGitRepo
    ) -> None:
        """``list_branches`` 返回统一分支列表。"""
        branches = await adapter.list_branches(
            _repo_url(multi_branch_repo.path), {"type": "NONE"}
        )
        assert branches == ["feature/test", "main"]

    @pytest.mark.asyncio
    async def test_get_default_branch_returns_unified_value(
        self, adapter: GitRepositoryAdapter, source_repo: LocalGitRepo
    ) -> None:
        """``get_default_branch`` 返回统一默认分支名。"""
        default_branch = await adapter.get_default_branch(
            _repo_url(source_repo.path), {"type": "NONE"}
        )
        assert default_branch == "main"


# --------------------------------------------------------------------------- #
# 验收 2：凭据不复制进绑定表
# --------------------------------------------------------------------------- #


class TestCredentialSecurity:
    """凭据明文不进入绑定表、事件 payload 或返回视图。"""

    @pytest.mark.asyncio
    async def test_https_token_not_in_binding_table(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        secret_service: LocalSecretService,
        source_repo: LocalGitRepo,
    ) -> None:
        """HTTPS token 明文不存入 project_repositories 表，只存 secret_id。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter, secret_service)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="HTTPS_TOKEN",
            credential_plaintext=_SECRET_TOKEN,
            actor_id=admin_id,
        )

        # 读取原始 DB 行
        row = await _read_binding_row(db, view["id"])
        assert row["credential_type"] == "HTTPS_TOKEN"
        assert row["credential_secret_id"] is not None
        assert row["credential_secret_id"] != ""
        # 明文 token 不在任何列中
        for col_name, col_value in row.items():
            assert _SECRET_TOKEN not in str(col_value), (
                f"plaintext token found in column {col_name!r}"
            )

    @pytest.mark.asyncio
    async def test_https_token_not_in_events(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        secret_service: LocalSecretService,
        source_repo: LocalGitRepo,
    ) -> None:
        """repository.bound 事件 payload 不含 HTTPS token 明文。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter, secret_service)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="HTTPS_TOKEN",
            credential_plaintext=_SECRET_TOKEN,
            actor_id=admin_id,
        )

        events = await _list_outbox_events(db, project_id)
        bound_events = [e for e in events if e["event_type"] == "repository.bound"]
        assert len(bound_events) == 1
        blob = json.dumps(bound_events[0], ensure_ascii=False)
        assert _SECRET_TOKEN not in blob, "plaintext token leaked into event payload"

    @pytest.mark.asyncio
    async def test_https_token_not_in_view(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        secret_service: LocalSecretService,
        source_repo: LocalGitRepo,
    ) -> None:
        """``bind_repository`` 返回视图不含 HTTPS token 明文。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter, secret_service)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="HTTPS_TOKEN",
            credential_plaintext=_SECRET_TOKEN,
            actor_id=admin_id,
        )

        blob = json.dumps(view, ensure_ascii=False)
        assert _SECRET_TOKEN not in blob, "plaintext token leaked into view"
        # credential_configured 为 True，但不暴露 secret_id
        assert view["credential_configured"] is True
        assert "credential_secret_id" not in view

    @pytest.mark.asyncio
    async def test_ssh_key_path_stored_not_content(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        ssh_key_file: Path,
        source_repo: LocalGitRepo,
    ) -> None:
        """SSH key 路径存入绑定表（路径不是密钥），密钥内容不存入。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="SSH_KEY",
            ssh_key_path=str(ssh_key_file),
            actor_id=admin_id,
        )

        row = await _read_binding_row(db, view["id"])
        assert row["credential_type"] == "SSH_KEY"
        assert row["ssh_key_path"] == str(ssh_key_file)
        # 密钥内容不在任何列中
        for col_name, col_value in row.items():
            assert _SSH_KEY_CONTENT not in str(col_value), (
                f"key content found in column {col_name!r}"
            )

    @pytest.mark.asyncio
    async def test_ssh_key_content_not_in_events(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        ssh_key_file: Path,
        source_repo: LocalGitRepo,
    ) -> None:
        """SSH 密钥内容不出现在事件 payload 中。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="SSH_KEY",
            ssh_key_path=str(ssh_key_file),
            actor_id=admin_id,
        )

        events = await _list_outbox_events(db, project_id)
        blob = json.dumps(events, ensure_ascii=False)
        assert _SSH_KEY_CONTENT not in blob, "key content leaked into events"

    @pytest.mark.asyncio
    async def test_none_credential_stores_nothing(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """NONE 凭据模式不存储任何凭据引用。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )

        row = await _read_binding_row(db, view["id"])
        assert row["credential_type"] == "NONE"
        assert row["credential_secret_id"] is None
        assert row["ssh_key_path"] is None
        assert view["credential_configured"] is False

    @pytest.mark.asyncio
    async def test_verify_event_does_not_leak_token(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        secret_service: LocalSecretService,
        source_repo: LocalGitRepo,
    ) -> None:
        """``repository.verified`` 事件 payload 不含 HTTPS token 明文。

        绑定 HTTPS_TOKEN 后验证（虽然本地 file:// 仓库不需要 token，但
        SecretService 会被调用解析 token），验证事件不应泄漏明文。
        """
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter, secret_service)

        # 绑定 HTTPS_TOKEN（虽然 URL 是本地 file://，token 会被存储）
        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="HTTPS_TOKEN",
            credential_plaintext=_SECRET_TOKEN,
            actor_id=admin_id,
        )

        # 验证绑定（adapter 会用 NONE 凭据 clone，因为本地 file:// 不需要 token；
        # 但 _resolve_credentials 会解析 token）
        await service.verify_binding(view["id"], actor_id=admin_id)

        events = await _list_outbox_events(db, project_id)
        verified_events = [
            e for e in events if e["event_type"] == "repository.verified"
        ]
        assert len(verified_events) == 1
        blob = json.dumps(verified_events[0], ensure_ascii=False)
        assert _SECRET_TOKEN not in blob, "token leaked into verified event"


# --------------------------------------------------------------------------- #
# 验收 3：验证只做安全探测，不修改主分支
# --------------------------------------------------------------------------- #


class TestSafeProbing:
    """``verify`` 只做安全探测，不修改源仓库主分支或创建新分支。"""

    @pytest.mark.asyncio
    async def test_verify_does_not_modify_main_branch_head(
        self,
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """验证后源仓库 main 分支 HEAD commit 不变。"""
        head_before = source_repo.rev_parse("HEAD")

        await adapter.verify(
            _repo_url(source_repo.path),
            {"type": "NONE"},
            expected_branch="main",
        )

        head_after = source_repo.rev_parse("HEAD")
        assert head_before == head_after, "main branch HEAD was modified by verify"

    @pytest.mark.asyncio
    async def test_verify_does_not_create_verify_branch_on_remote(
        self,
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """验证不在源仓库创建 ``_maf_verify_*`` 分支。"""
        branches_before = await _list_remote_branches(source_repo)

        await adapter.verify(
            _repo_url(source_repo.path),
            {"type": "NONE"},
            expected_branch="main",
        )

        branches_after = await _list_remote_branches(source_repo)
        assert branches_before == branches_after, (
            f"branch list changed: before={branches_before}, after={branches_after}"
        )
        # 确保没有 _maf_verify_ 分支
        assert not any(b.startswith("_maf_verify_") for b in branches_after), (
            f"_maf_verify_ branch created on source repo: {branches_after}"
        )

    @pytest.mark.asyncio
    async def test_verify_does_not_modify_remote_refs(
        self,
        adapter: GitRepositoryAdapter,
        multi_branch_repo: LocalGitRepo,
    ) -> None:
        """验证后源仓库所有 refs 不变（含 main 和 feature/test）。"""
        main_before = multi_branch_repo.rev_parse("refs/heads/main")
        feature_before = multi_branch_repo.rev_parse("refs/heads/feature/test")

        await adapter.verify(
            _repo_url(multi_branch_repo.path),
            {"type": "NONE"},
            expected_branch="main",
        )

        main_after = multi_branch_repo.rev_parse("refs/heads/main")
        feature_after = multi_branch_repo.rev_parse("refs/heads/feature/test")
        assert main_before == main_after
        assert feature_before == feature_after

    @pytest.mark.asyncio
    async def test_verify_via_service_does_not_modify_main_branch(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """通过 RepositoryBindingService.verify_binding 验证后源仓库 HEAD 不变。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )

        head_before = source_repo.rev_parse("HEAD")
        await service.verify_binding(view["id"], actor_id=admin_id)
        head_after = source_repo.rev_parse("HEAD")
        assert head_before == head_after

    @pytest.mark.asyncio
    async def test_verify_cleans_up_temp_clone(
        self,
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """验证后临时 clone 目录被清理。"""
        workspace_root = adapter._workspace_root  # type: ignore[attr-defined]

        await adapter.verify(
            _repo_url(source_repo.path),
            {"type": "NONE"},
            expected_branch="main",
        )

        # workspace_root 下不应残留 _maf_verify_ 目录
        remaining = [
            p.name for p in workspace_root.iterdir()
            if p.is_dir() and p.name.startswith("_maf_verify_")
        ]
        assert remaining == [], f"temp clone dirs not cleaned up: {remaining}"


# --------------------------------------------------------------------------- #
# GitRepositoryAdapter 直接测试
# --------------------------------------------------------------------------- #


class TestGitRepositoryAdapter:
    """``GitRepositoryAdapter`` 直接测试（不经 service 层）。"""

    @pytest.mark.asyncio
    async def test_verify_success_returns_verified_true(
        self, adapter: GitRepositoryAdapter, source_repo: LocalGitRepo
    ) -> None:
        """成功验证返回 VerifyResult(verified=True)。"""
        result = await adapter.verify(
            _repo_url(source_repo.path),
            {"type": "NONE"},
            expected_branch="main",
        )
        assert result.verified is True
        assert result.repository_info is not None
        assert result.repository_info["default_branch"] == "main"
        assert "main" in result.repository_info["branches"]
        assert result.repository_info["can_read"] is True

    @pytest.mark.asyncio
    async def test_verify_branch_not_found(
        self, adapter: GitRepositoryAdapter, source_repo: LocalGitRepo
    ) -> None:
        """期望分支不存在返回 verified=False，repository_info 含可用分支列表。"""
        result = await adapter.verify(
            _repo_url(source_repo.path),
            {"type": "NONE"},
            expected_branch="nonexistent-branch",
        )
        assert result.verified is False
        assert result.error is not None
        assert "nonexistent-branch" in result.error
        # repository_info 仍返回（含 default_branch 和 branches）
        assert result.repository_info is not None
        assert result.repository_info["can_read"] is True
        assert result.repository_info["can_write"] is False

    @pytest.mark.asyncio
    async def test_verify_unreachable_url(
        self, adapter: GitRepositoryAdapter
    ) -> None:
        """不可达 URL 返回 verified=False，repository_info=None。"""
        result = await adapter.verify(
            "file:///nonexistent/path/to/repo.git",
            {"type": "NONE"},
            expected_branch="main",
        )
        assert result.verified is False
        assert result.repository_info is None
        assert result.error is not None
        assert "clone failed" in result.error

    @pytest.mark.asyncio
    async def test_verify_empty_url_rejected(
        self, adapter: GitRepositoryAdapter
    ) -> None:
        """空 URL 返回 verified=False。"""
        result = await adapter.verify("", {"type": "NONE"})
        assert result.verified is False
        assert "repository_url" in result.error

    @pytest.mark.asyncio
    async def test_verify_none_expected_branch_skips_branch_check(
        self, adapter: GitRepositoryAdapter, source_repo: LocalGitRepo
    ) -> None:
        """expected_branch=None 时不检查特定分支，直接返回 verified=True。"""
        result = await adapter.verify(
            _repo_url(source_repo.path),
            {"type": "NONE"},
            expected_branch=None,
        )
        assert result.verified is True
        assert result.repository_info is not None
        assert result.repository_info["default_branch"] == "main"

    @pytest.mark.asyncio
    async def test_list_branches_success(
        self, adapter: GitRepositoryAdapter, multi_branch_repo: LocalGitRepo
    ) -> None:
        """``list_branches`` 返回所有分支。"""
        branches = await adapter.list_branches(
            _repo_url(multi_branch_repo.path), {"type": "NONE"}
        )
        assert "main" in branches
        assert "feature/test" in branches

    @pytest.mark.asyncio
    async def test_get_default_branch_success(
        self, adapter: GitRepositoryAdapter, source_repo: LocalGitRepo
    ) -> None:
        """``get_default_branch`` 返回 ``main``。"""
        default = await adapter.get_default_branch(
            _repo_url(source_repo.path), {"type": "NONE"}
        )
        assert default == "main"


# --------------------------------------------------------------------------- #
# bind_repository 测试
# --------------------------------------------------------------------------- #


class TestBindRepository:
    """``bind_repository`` 成功路径与错误路径测试。"""

    @pytest.mark.asyncio
    async def test_bind_none_credential_success(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """NONE 凭据绑定成功，返回 version=1, verified=False。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )

        assert view["project_id"] == project_id
        assert view["repository_url"] == _repo_url(source_repo.path)
        assert view["branch"] == "main"
        assert view["credential_type"] == "NONE"
        assert view["credential_configured"] is False
        assert view["verified"] is False
        assert view["verified_at"] is None
        assert view["bound_by"] == admin_id
        assert view["bound_at"] is not None
        assert view["version"] == 1
        uuid.UUID(view["id"])

    @pytest.mark.asyncio
    async def test_bind_writes_repository_bound_event(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """绑定写入 ``repository.bound`` 事件到 outbox。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )

        events = await _list_outbox_events(db, project_id)
        bound_events = [e for e in events if e["event_type"] == "repository.bound"]
        assert len(bound_events) == 1
        assert bound_events[0]["aggregate_type"] == "repository_binding"
        assert bound_events[0]["aggregate_id"] == view["id"]
        assert bound_events[0]["payload"]["project_id"] == project_id
        assert bound_events[0]["payload"]["branch"] == "main"
        assert bound_events[0]["payload"]["credential_type"] == "NONE"
        assert bound_events[0]["payload"]["bound_by"] == admin_id

    @pytest.mark.asyncio
    async def test_bind_observer_forbidden(
        self,
        observer_db: tuple[Database, str, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """OBSERVER 无 write repositories 权限，绑定被拒。"""
        db, admin_id, observer_id = observer_db
        project_id = await _create_project_with_members(db, admin_id, observer_id)
        service = _make_service(db, adapter)

        with pytest.raises(PermissionDeniedError):
            await service.bind_repository(
                project_id,
                _repo_url(source_repo.path),
                "main",
                credential_type="NONE",
                actor_id=observer_id,
            )

    @pytest.mark.asyncio
    async def test_bind_non_member_not_found(
        self,
        owner_nonmember_db: tuple[Database, str, str, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """有 OWNER 全局权限但非项目成员，绑定返回 404（不泄露项目存在性）。"""
        db, admin_id, observer_id, owner_nm_id = owner_nonmember_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        with pytest.raises(NotFoundError):
            await service.bind_repository(
                project_id,
                _repo_url(source_repo.path),
                "main",
                credential_type="NONE",
                actor_id=owner_nm_id,
            )

    @pytest.mark.asyncio
    async def test_bind_empty_url_rejected(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
    ) -> None:
        """空 URL 抛 ArgumentError。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        with pytest.raises(ArgumentError):
            await service.bind_repository(
                project_id,
                "   ",
                "main",
                credential_type="NONE",
                actor_id=admin_id,
            )

    @pytest.mark.asyncio
    async def test_bind_empty_branch_rejected(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """空分支名抛 ArgumentError。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        with pytest.raises(ArgumentError):
            await service.bind_repository(
                project_id,
                _repo_url(source_repo.path),
                "",
                credential_type="NONE",
                actor_id=admin_id,
            )

    @pytest.mark.asyncio
    async def test_bind_invalid_credential_type_rejected(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """非法凭据类型抛 ArgumentError。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        with pytest.raises(ArgumentError):
            await service.bind_repository(
                project_id,
                _repo_url(source_repo.path),
                "main",
                credential_type="INVALID_TYPE",
                actor_id=admin_id,
            )

    @pytest.mark.asyncio
    async def test_bind_https_without_plaintext_rejected(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """HTTPS_TOKEN 模式缺 plaintext 抛 ArgumentError。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        with pytest.raises(ArgumentError):
            await service.bind_repository(
                project_id,
                _repo_url(source_repo.path),
                "main",
                credential_type="HTTPS_TOKEN",
                credential_plaintext=None,
                actor_id=admin_id,
            )

    @pytest.mark.asyncio
    async def test_bind_ssh_without_key_path_rejected(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """SSH_KEY 模式缺 ssh_key_path 抛 ArgumentError。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        with pytest.raises(ArgumentError):
            await service.bind_repository(
                project_id,
                _repo_url(source_repo.path),
                "main",
                credential_type="SSH_KEY",
                ssh_key_path=None,
                actor_id=admin_id,
            )

    @pytest.mark.asyncio
    async def test_bind_none_with_credentials_rejected(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """NONE 模式不应提供凭据。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        with pytest.raises(ArgumentError):
            await service.bind_repository(
                project_id,
                _repo_url(source_repo.path),
                "main",
                credential_type="NONE",
                credential_plaintext="should-not-be-here",
                actor_id=admin_id,
            )


# --------------------------------------------------------------------------- #
# verify_binding 测试
# --------------------------------------------------------------------------- #


class TestVerifyBinding:
    """``verify_binding`` 成功路径与错误路径测试。"""

    @pytest.mark.asyncio
    async def test_verify_success_updates_verified_flag(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """验证成功后 verified=True, verified_at 已设置, version 递增。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )
        assert view["verified"] is False
        assert view["version"] == 1

        verified_view = await service.verify_binding(view["id"], actor_id=admin_id)

        assert verified_view["verified"] is True
        assert verified_view["verified_at"] is not None
        assert verified_view["version"] == 2

    @pytest.mark.asyncio
    async def test_verify_writes_repository_verified_event(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """验证写入 ``repository.verified`` 事件。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )

        await service.verify_binding(view["id"], actor_id=admin_id)

        events = await _list_outbox_events(db, project_id)
        verified_events = [
            e for e in events if e["event_type"] == "repository.verified"
        ]
        assert len(verified_events) == 1
        assert verified_events[0]["aggregate_id"] == view["id"]
        assert verified_events[0]["payload"]["verified"] is True
        assert verified_events[0]["payload"]["default_branch"] == "main"
        assert verified_events[0]["payload"]["can_read"] is True

    @pytest.mark.asyncio
    async def test_verify_failure_unreachable_url(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
    ) -> None:
        """URL 不可达时 verified=False，事件记录失败。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        view = await service.bind_repository(
            project_id,
            "file:///nonexistent/path/to/repo.git",
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )

        verified_view = await service.verify_binding(view["id"], actor_id=admin_id)

        assert verified_view["verified"] is False
        assert verified_view["verified_at"] is not None

        events = await _list_outbox_events(db, project_id)
        verified_events = [
            e for e in events if e["event_type"] == "repository.verified"
        ]
        assert len(verified_events) == 1
        assert verified_events[0]["payload"]["verified"] is False
        assert verified_events[0]["payload"]["error"] is not None

    @pytest.mark.asyncio
    async def test_verify_failure_branch_not_found(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """绑定分支不存在时 verified=False。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "nonexistent-branch",
            credential_type="NONE",
            actor_id=admin_id,
        )

        verified_view = await service.verify_binding(view["id"], actor_id=admin_id)

        assert verified_view["verified"] is False
        events = await _list_outbox_events(db, project_id)
        verified_events = [
            e for e in events if e["event_type"] == "repository.verified"
        ]
        assert verified_events[0]["payload"]["verified"] is False
        assert "nonexistent-branch" in verified_events[0]["payload"]["error"]

    @pytest.mark.asyncio
    async def test_verify_observer_forbidden(
        self,
        observer_db: tuple[Database, str, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """OBSERVER 无 write repositories 权限，验证被拒。"""
        db, admin_id, observer_id = observer_db
        project_id = await _create_project_with_members(db, admin_id, observer_id)
        service = _make_service(db, adapter)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )

        with pytest.raises(PermissionDeniedError):
            await service.verify_binding(view["id"], actor_id=observer_id)

    @pytest.mark.asyncio
    async def test_verify_nonexistent_binding_not_found(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
    ) -> None:
        """验证不存在的绑定 ID 抛 NotFoundError。"""
        db, admin_id = admin_db
        service = _make_service(db, adapter)

        with pytest.raises(NotFoundError):
            await service.verify_binding("nonexistent-id", actor_id=admin_id)

    @pytest.mark.asyncio
    async def test_verify_non_member_not_found(
        self,
        owner_nonmember_db: tuple[Database, str, str, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """有 OWNER 全局权限但非项目成员，验证返回 404。"""
        db, admin_id, observer_id, owner_nm_id = owner_nonmember_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )

        with pytest.raises(NotFoundError):
            await service.verify_binding(view["id"], actor_id=owner_nm_id)


# --------------------------------------------------------------------------- #
# list_bindings 测试
# --------------------------------------------------------------------------- #


class TestListBindings:
    """``list_bindings`` 成功路径与权限测试。"""

    @pytest.mark.asyncio
    async def test_list_returns_all_bindings(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
        multi_branch_repo: LocalGitRepo,
    ) -> None:
        """列出项目的全部绑定。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )
        await service.bind_repository(
            project_id,
            _repo_url(multi_branch_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )

        bindings = await service.list_bindings(project_id, actor_id=admin_id)
        assert len(bindings) == 2
        urls = {b["repository_url"] for b in bindings}
        assert _repo_url(source_repo.path) in urls
        assert _repo_url(multi_branch_repo.path) in urls

    @pytest.mark.asyncio
    async def test_list_observer_allowed(
        self,
        observer_db: tuple[Database, str, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """OBSERVER 有 read repositories 权限，可以列绑定。"""
        db, admin_id, observer_id = observer_db
        project_id = await _create_project_with_members(db, admin_id, observer_id)
        service = _make_service(db, adapter)

        await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )

        bindings = await service.list_bindings(project_id, actor_id=observer_id)
        assert len(bindings) == 1

    @pytest.mark.asyncio
    async def test_list_non_member_not_found(
        self,
        owner_nonmember_db: tuple[Database, str, str, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """非项目成员列绑定返回 404。"""
        db, admin_id, observer_id, owner_nm_id = owner_nonmember_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )

        with pytest.raises(NotFoundError):
            await service.list_bindings(project_id, actor_id=owner_nm_id)

    @pytest.mark.asyncio
    async def test_list_empty_project_returns_empty_list(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
    ) -> None:
        """无绑定的项目返回空列表。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        bindings = await service.list_bindings(project_id, actor_id=admin_id)
        assert bindings == []


# --------------------------------------------------------------------------- #
# remove_binding 测试
# --------------------------------------------------------------------------- #


class TestRemoveBinding:
    """``remove_binding`` 成功路径与权限测试。"""

    @pytest.mark.asyncio
    async def test_remove_success(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """移除绑定后 get_binding 返回 None。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )

        await service.remove_binding(view["id"], actor_id=admin_id)

        row = await _read_binding_row(db, view["id"])
        assert row == {}, "binding should be physically deleted"

    @pytest.mark.asyncio
    async def test_remove_writes_repository_removed_event(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """移除写入 ``repository.removed`` 事件。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )

        await service.remove_binding(view["id"], actor_id=admin_id)

        events = await _list_outbox_events(db, project_id)
        removed_events = [
            e for e in events if e["event_type"] == "repository.removed"
        ]
        assert len(removed_events) == 1
        assert removed_events[0]["aggregate_id"] == view["id"]
        assert removed_events[0]["payload"]["removed_by"] == admin_id
        assert removed_events[0]["payload"]["repository_url"] == _repo_url(
            source_repo.path
        )

    @pytest.mark.asyncio
    async def test_remove_observer_forbidden(
        self,
        observer_db: tuple[Database, str, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """OBSERVER 无 write repositories 权限，移除被拒。"""
        db, admin_id, observer_id = observer_db
        project_id = await _create_project_with_members(db, admin_id, observer_id)
        service = _make_service(db, adapter)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )

        with pytest.raises(PermissionDeniedError):
            await service.remove_binding(view["id"], actor_id=observer_id)

    @pytest.mark.asyncio
    async def test_remove_nonexistent_binding_not_found(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
    ) -> None:
        """移除不存在的绑定 ID 抛 NotFoundError。"""
        db, admin_id = admin_db
        service = _make_service(db, adapter)

        with pytest.raises(NotFoundError):
            await service.remove_binding("nonexistent-id", actor_id=admin_id)

    @pytest.mark.asyncio
    async def test_remove_non_member_not_found(
        self,
        owner_nonmember_db: tuple[Database, str, str, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """有 OWNER 全局权限但非项目成员，移除返回 404。"""
        db, admin_id, observer_id, owner_nm_id = owner_nonmember_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )

        with pytest.raises(NotFoundError):
            await service.remove_binding(view["id"], actor_id=owner_nm_id)


# --------------------------------------------------------------------------- #
# 事件与事务一致性
# --------------------------------------------------------------------------- #


class TestEventTransactionConsistency:
    """事件与业务写入同事务：事务成功事件落库。"""

    @pytest.mark.asyncio
    async def test_bind_and_verify_events_in_order(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """绑定 → 验证事件按顺序写入 outbox。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )
        await service.verify_binding(view["id"], actor_id=admin_id)

        events = await _list_outbox_events(db, project_id)
        repository_events = [
            e for e in events if e["event_type"].startswith("repository.")
        ]
        assert len(repository_events) == 2
        assert repository_events[0]["event_type"] == "repository.bound"
        assert repository_events[1]["event_type"] == "repository.verified"
        assert repository_events[1]["payload"]["verified"] is True

    @pytest.mark.asyncio
    async def test_full_lifecycle_events(
        self,
        admin_db: tuple[Database, str],
        adapter: GitRepositoryAdapter,
        source_repo: LocalGitRepo,
    ) -> None:
        """完整生命周期：bind → verify → remove 产生 3 个事件。"""
        db, admin_id = admin_db
        project_id = await _create_project_with_members(db, admin_id)
        service = _make_service(db, adapter)

        view = await service.bind_repository(
            project_id,
            _repo_url(source_repo.path),
            "main",
            credential_type="NONE",
            actor_id=admin_id,
        )
        await service.verify_binding(view["id"], actor_id=admin_id)
        await service.remove_binding(view["id"], actor_id=admin_id)

        events = await _list_outbox_events(db, project_id)
        repository_events = [
            e for e in events if e["event_type"].startswith("repository.")
        ]
        assert len(repository_events) == 3
        assert repository_events[0]["event_type"] == "repository.bound"
        assert repository_events[1]["event_type"] == "repository.verified"
        assert repository_events[2]["event_type"] == "repository.removed"
