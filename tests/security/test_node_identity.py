"""TASK-020 测试：验证节点 Git 身份。

验收标准覆盖（对应 TASK-020 文档与任务说明）：

1. **合法节点事件验证通过**：已注册节点 + commit author email 与 manifest
   声明一致 → ``verify_node_identity`` 返回 ``NodeIdentity(verified=True)``。
2. **未知节点拒绝**：未注册节点的非 ``NODE_REGISTERED`` 事件被拒绝
   （``EVENT_NODE_UNKNOWN``）。
3. **source 不匹配拒绝**：``event.node_id`` 与 ``expected_node_id`` 不一致
   → ``EVENT_NODE_IDENTITY_MISMATCH``（冒用其他 node_id 被拒绝）。
4. **commit author 不匹配拒绝**：commit author email 与 manifest 声明 email
   不一致 → ``EVENT_NODE_IDENTITY_MISMATCH``。
5. **自动注册禁止**：未知节点拒绝后 ``maf/control:.maf/nodes/<node-id>.yaml``
   不被创建。
6. **NODE_REGISTERED 事件使用 payload manifest**：首次注册时使用事件 payload
   中的 manifest 作为声明身份（trust-on-first-use）。
7. **空 expected_node_id 拒绝**：``ArgumentError``。
8. **manifest node_id 不匹配拒绝**：control 上的 manifest node_id 与 expected
   不一致 → ``EVENT_NODE_IDENTITY_MISMATCH``。

附加单元测试：
- ``extract_node_identity_from_manifest`` / ``verify_commit_author`` 辅助函数。

测试使用 ``tests/fixtures/git_repo.py`` 的 ``init_local_git_repo`` 创建真实
git 仓库，用 TASK-015 的 ``initialize_project`` 初始化 control 分支，再通过
worktree 直接在 ``maf/node/<node-id>`` 分支写入事件文件、在 ``maf/control``
分支写入节点清单。所有异步入口经 ``asyncio.run`` 同步执行。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

# packages/artifact_schemas/src 尚未加入 pyproject.toml pythonpath（TASK-002 范围），
# 此处显式添加，使 maf_artifact_schemas 可被 maf_server.git_coordination.schemas
# 与 maf_server.modules.git_coordination.service 导入。与现有
# tests/contract/test_control_reader.py 一致。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT_SCHEMAS_SRC = _PROJECT_ROOT / "packages" / "artifact_schemas" / "src"
if str(_ARTIFACT_SCHEMAS_SRC) not in sys.path:
    sys.path.insert(0, str(_ARTIFACT_SCHEMAS_SRC))

from maf_contracts.coordination import CoordinationEventModel  # noqa: E402
from maf_domain.errors import ArgumentError, ErrorCode, ReasonCode  # noqa: E402
from maf_repository_adapters import SubprocessGitCli  # noqa: E402
from maf_server.core.security import (  # noqa: E402
    extract_node_identity_from_manifest,
    verify_commit_author,
)
from maf_server.git_coordination.schemas import SchemaLoader  # noqa: E402
from maf_server.modules.git_coordination.service import (  # noqa: E402
    LocalGitCoordinationService,
    NodeIdentity,
    NodeIdentityError,
)

# 导入 tests/fixtures/git_repo.py 的 init_local_git_repo 工厂。
_FIXTURES_DIR = _PROJECT_ROOT / "tests" / "fixtures"
if str(_FIXTURES_DIR) not in sys.path:
    sys.path.insert(0, str(_FIXTURES_DIR))
from git_repo import init_local_git_repo  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

_NODE_ID_A = "node-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_NODE_ID_B = "node-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_CONTROL_COMMIT = "abcdef1234567890abcdef1234567890abcdef12"
_TEMPLATES_DIR = _PROJECT_ROOT / "templates" / "git_coordination"

_DEFAULT_AUTHOR_NAME = "Test Bot"
_DEFAULT_AUTHOR_EMAIL = "bot@example.test"
_ATTACKER_AUTHOR_NAME = "Attacker"
_ATTACKER_AUTHOR_EMAIL = "attacker@evil.test"


def _git_env(
    author_name: str = _DEFAULT_AUTHOR_NAME,
    author_email: str = _DEFAULT_AUTHOR_EMAIL,
) -> dict[str, str]:
    """构造隔离的 git 身份环境，允许自定义 author。"""
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
        "GIT_TERMINAL_PROMPT": "0",
    }


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #


def _run(coro: Any) -> Any:
    """在独立事件循环中执行协程并返回结果（与现有集成测试风格一致）。"""
    return asyncio.run(coro)


def _git(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> str:
    """同步执行 git 命令，返回 stdout（用于 fixture 准备与断言）。"""
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=env or _git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {args!r} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def _make_manifest(
    *,
    node_id: str = _NODE_ID_A,
    display_name: str = "Test Node A",
    git_name: str = _DEFAULT_AUTHOR_NAME,
    git_email: str = _DEFAULT_AUTHOR_EMAIL,
    status: str = "ACTIVE",
    capabilities: list[str] | None = None,
    capacity: int = 4,
) -> dict[str, Any]:
    """构造一个合法的 NodeManifest dict。"""
    return {
        "schema_version": 1,
        "node_id": node_id,
        "display_name": display_name,
        "git_identity": {
            "name": git_name,
            "email": git_email,
        },
        "capabilities": capabilities if capabilities is not None else ["python", "docker"],
        "model_aliases": [],
        "docker_profiles": ["generic"],
        "capacity": capacity,
        "status": status,
        "software_version": "0.1.0",
        "version": 1,
        "generated_at": "2026-07-17T00:00:00Z",
    }


def _make_event(
    *,
    event_type: str = "CLAIM_REQUESTED",
    node_id: str = _NODE_ID_A,
    task_id: str | None = "TASK-001",
    assignment_id: str | None = None,
    assignment_epoch: int | None = None,
    based_on_control_commit: str = _CONTROL_COMMIT,
    event_id: str | None = None,
    occurred_at: str = "2026-07-17T00:00:00Z",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一个合法的 CoordinationEvent dict。"""
    return {
        "schema_version": 1,
        "event_id": event_id or f"evt-{uuid.uuid4()}",
        "event_type": event_type,
        "node_id": node_id,
        "task_id": task_id,
        "assignment_id": assignment_id,
        "assignment_epoch": assignment_epoch,
        "based_on_control_commit": based_on_control_commit,
        "occurred_at": occurred_at,
        "payload": payload or {"note": "test event"},
    }


def _make_node_registered_event(
    *,
    node_id: str = _NODE_ID_A,
    manifest: dict[str, Any] | None = None,
    event_id: str | None = None,
    based_on_control_commit: str = _CONTROL_COMMIT,
) -> dict[str, Any]:
    """构造一个 NODE_REGISTERED 事件，payload 含 manifest。"""
    if manifest is None:
        manifest = _make_manifest(node_id=node_id)
    return _make_event(
        event_type="NODE_REGISTERED",
        node_id=node_id,
        task_id=None,
        event_id=event_id,
        based_on_control_commit=based_on_control_commit,
        payload={"manifest": manifest, "environment": {"hostname": "test-host"}},
    )


def _write_event_to_node_branch(
    repo: Path,
    node_id: str,
    event: dict[str, Any],
    *,
    author_name: str = _DEFAULT_AUTHOR_NAME,
    author_email: str = _DEFAULT_AUTHOR_EMAIL,
    content: str | None = None,
) -> str:
    """Write an event file to ``maf/node/<node-id>`` branch, return new HEAD.

    Uses a temporary worktree to avoid disturbing the current branch. Creates
    an orphan branch on first write. Allows specifying a custom git author
    for commit author verification tests.
    """
    env = _git_env(author_name, author_email)
    branch = f"maf/node/{node_id}"
    event_id = event["event_id"]
    rel_path = f".maf/events/{event_id}.json"
    wt = repo / ".maf-event-wt"

    # Clean up any stale worktree from a previous test/failed assertion.
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
        env=env,
        capture_output=True,
    )

    branch_exists = subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
         f"refs/heads/{branch}"],
        env=env,
    ).returncode == 0

    if not branch_exists:
        _git(repo, "worktree", "add", "--detach", str(wt), "HEAD", env=env)
        _git(wt, "switch", "--orphan", branch, env=env)
    else:
        _git(repo, "worktree", "add", str(wt), branch, env=env)

    event_path = wt / rel_path
    event_path.parent.mkdir(parents=True, exist_ok=True)
    file_content = content if content is not None else (
        json.dumps(event, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )
    event_path.write_text(file_content, encoding="utf-8")

    _git(wt, "add", "--", rel_path, env=env)
    _git(wt, "commit", "-q", "-m", f"event: {event_id}", env=env)
    new_head = _git(wt, "rev-parse", "HEAD", env=env)

    _git(repo, "worktree", "remove", "--force", str(wt), env=env)
    return new_head


def _write_node_manifest_to_control(
    repo: Path,
    node_id: str,
    manifest: dict[str, Any],
) -> str:
    """Write a node manifest YAML to ``maf/control:.maf/nodes/<node-id>.yaml``.

    Uses a temporary worktree on the control branch. The commit author is the
    default test author (central scheduler identity, not the node's).
    """
    env = _git_env()
    branch = "maf/control"
    rel_path = f".maf/nodes/{node_id}.yaml"
    wt = repo / ".maf-control-wt"

    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
        env=env,
        capture_output=True,
    )

    _git(repo, "worktree", "add", str(wt), branch, env=env)

    manifest_path = wt / rel_path
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_content = yaml.dump(manifest, default_flow_style=False, sort_keys=False)
    manifest_path.write_text(yaml_content, encoding="utf-8")

    _git(wt, "add", "--", rel_path, env=env)
    _git(wt, "commit", "-q", "-m", f"register node {node_id}", env=env)
    new_head = _git(wt, "rev-parse", "HEAD", env=env)

    _git(repo, "worktree", "remove", "--force", str(wt), env=env)
    return new_head


def _control_file_exists(repo: Path, path: str) -> bool:
    """Check whether ``maf/control:<path>`` exists."""
    rc = subprocess.run(
        ["git", "-C", str(repo), "show", f"maf/control:{path}"],
        env=_git_env(),
        capture_output=True,
    ).returncode
    return rc == 0


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def templates_dir() -> Path:
    """返回项目内置的 ``templates/git_coordination``。"""
    return _TEMPLATES_DIR


@pytest.fixture()
def schema_loader() -> SchemaLoader:
    """使用默认 ``templates/git_coordination/schemas/`` 的 SchemaLoader。"""
    return SchemaLoader()


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """使用 ``init_local_git_repo`` 初始化一个真实 git 仓库。"""
    repo_path = tmp_path / "repo"
    return init_local_git_repo(repo_path).path


@pytest.fixture()
def git_cli(git_repo: Path) -> SubprocessGitCli:
    """绑定到 ``git_repo`` 的 SubprocessGitCli。"""
    return SubprocessGitCli(allowed_roots=[git_repo])


@pytest.fixture(autouse=True)
def _clear_maf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除 ``MAF_*`` 环境变量，避免本地 .env 污染测试。"""
    for key in list(os.environ):
        if key.startswith("MAF_"):
            monkeypatch.delenv(key, raising=False)


def _make_service(
    *,
    git_cli: SubprocessGitCli,
    repository_path: str,
    templates_dir: Path,
    schema_loader: SchemaLoader,
) -> LocalGitCoordinationService:
    return LocalGitCoordinationService(
        git_cli=git_cli,
        repository_path=repository_path,
        templates_dir=templates_dir,
        schema_loader=schema_loader,
    )


def _init_control(
    service: LocalGitCoordinationService,
    *,
    project_id: str = "proj-test-001",
    binding_id: str = "binding-1",
) -> str:
    """调用 ``initialize_project`` 创建 control 分支，返回 commit。"""
    return _run(service.initialize_project(binding_id, project_id))


# --------------------------------------------------------------------------- #
# 单元测试：security.py 辅助函数
# --------------------------------------------------------------------------- #


class TestExtractNodeIdentityFromManifest:
    """``extract_node_identity_from_manifest`` 从清单提取 Git 身份。"""

    def test_extracts_name_and_email(self) -> None:
        manifest = _make_manifest(git_name="Alice", git_email="alice@test")
        identity = extract_node_identity_from_manifest(manifest)
        assert identity == {"name": "Alice", "email": "alice@test"}

    def test_missing_git_identity_returns_empty(self) -> None:
        manifest = {"node_id": "node-x", "git_identity": {}}
        identity = extract_node_identity_from_manifest(manifest)
        assert identity == {"name": "", "email": ""}

    def test_missing_manifest_field_returns_empty(self) -> None:
        identity = extract_node_identity_from_manifest({})  # type: ignore[arg-type]
        assert identity == {"name": "", "email": ""}

    def test_non_dict_git_identity_returns_empty(self) -> None:
        manifest = {"git_identity": "not-a-dict"}
        identity = extract_node_identity_from_manifest(manifest)  # type: ignore[arg-type]
        assert identity == {"name": "", "email": ""}


class TestVerifyCommitAuthor:
    """``verify_commit_author`` 比较 commit author email 与声明身份。"""

    def test_matching_email_returns_true(self) -> None:
        commit_author = {"name": "Bot", "email": "bot@example.test"}
        declared = {"name": "Test Bot", "email": "bot@example.test"}
        assert verify_commit_author(commit_author, declared) is True

    def test_mismatched_email_returns_false(self) -> None:
        commit_author = {"name": "Bot", "email": "bot@example.test"}
        declared = {"name": "Bot", "email": "attacker@evil.test"}
        assert verify_commit_author(commit_author, declared) is False

    def test_empty_commit_email_returns_false(self) -> None:
        commit_author = {"name": "Bot", "email": ""}
        declared = {"name": "Bot", "email": "bot@example.test"}
        assert verify_commit_author(commit_author, declared) is False

    def test_empty_declared_email_returns_false(self) -> None:
        commit_author = {"name": "Bot", "email": "bot@example.test"}
        declared = {"name": "Bot", "email": ""}
        assert verify_commit_author(commit_author, declared) is False

    def test_case_sensitive_comparison(self) -> None:
        """Email 大小写敏感（与 git 内部行为一致）。"""
        commit_author = {"name": "Bot", "email": "Bot@Example.Test"}
        declared = {"name": "Bot", "email": "bot@example.test"}
        assert verify_commit_author(commit_author, declared) is False


# --------------------------------------------------------------------------- #
# 验收 1：合法节点事件验证通过
# --------------------------------------------------------------------------- #


class TestRegisteredNodeEventVerified:
    """已注册节点 + commit author 匹配 → 验证通过。"""

    def test_claim_event_verified(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """已注册节点的 CLAIM_REQUESTED 事件验证通过。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        # 注册节点 A：写 manifest 到 control 的 nodes/ 目录。
        manifest = _make_manifest(
            node_id=_NODE_ID_A,
            git_name=_DEFAULT_AUTHOR_NAME,
            git_email=_DEFAULT_AUTHOR_EMAIL,
        )
        _write_node_manifest_to_control(git_repo, _NODE_ID_A, manifest)

        # 在 maf/node/<A> 分支写入 CLAIM_REQUESTED 事件（author 与 manifest 一致）。
        event_dict = _make_event(
            event_type="CLAIM_REQUESTED",
            node_id=_NODE_ID_A,
            event_id="evt-claim-aaa-0001",
        )
        _write_event_to_node_branch(
            git_repo, _NODE_ID_A, event_dict,
            author_name=_DEFAULT_AUTHOR_NAME,
            author_email=_DEFAULT_AUTHOR_EMAIL,
        )

        event = CoordinationEventModel.model_validate(event_dict)
        result: NodeIdentity = _run(
            service.verify_node_identity(event, expected_node_id=_NODE_ID_A)
        )

        assert result["node_id"] == _NODE_ID_A
        assert result["verified"] is True
        assert result["verification_method"] == "commit_author_email"
        assert result["commit_author"]["email"] == _DEFAULT_AUTHOR_EMAIL
        assert result["commit_author"]["name"] == _DEFAULT_AUTHOR_NAME
        assert result["manifest"] is not None
        assert result["manifest"]["node_id"] == _NODE_ID_A
        assert result["failure_reason"] == ""

    def test_progress_event_verified(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """已注册节点的 PROGRESS_REPORTED 事件验证通过。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        manifest = _make_manifest(node_id=_NODE_ID_A)
        _write_node_manifest_to_control(git_repo, _NODE_ID_A, manifest)

        event_dict = _make_event(
            event_type="PROGRESS_REPORTED",
            node_id=_NODE_ID_A,
            event_id="evt-progress-aaa-0001",
            assignment_epoch=1,
        )
        _write_event_to_node_branch(git_repo, _NODE_ID_A, event_dict)

        event = CoordinationEventModel.model_validate(event_dict)
        result = _run(
            service.verify_node_identity(event, expected_node_id=_NODE_ID_A)
        )

        assert result["verified"] is True
        assert result["verification_method"] == "commit_author_email"


# --------------------------------------------------------------------------- #
# 验收 2：未知节点拒绝
# --------------------------------------------------------------------------- #


class TestUnknownNodeRejected:
    """未注册节点的非 NODE_REGISTERED 事件被拒绝。"""

    def test_unregistered_node_claim_rejected(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """未注册节点的 CLAIM_REQUESTED 事件被拒绝（EVENT_NODE_UNKNOWN）。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        # 不注册节点 A，直接写事件。
        event_dict = _make_event(
            event_type="CLAIM_REQUESTED",
            node_id=_NODE_ID_A,
            event_id="evt-unknown-aaa-0001",
        )
        _write_event_to_node_branch(git_repo, _NODE_ID_A, event_dict)

        event = CoordinationEventModel.model_validate(event_dict)
        with pytest.raises(NodeIdentityError) as exc_info:
            _run(service.verify_node_identity(event, expected_node_id=_NODE_ID_A))

        err = exc_info.value
        assert err.reason_code == ReasonCode.EVENT_NODE_UNKNOWN.value
        assert err.context["node_id"] == _NODE_ID_A
        assert err.context["failure_reason"] == "node_not_registered"
        assert err.context["event_id"] == event_dict["event_id"]
        assert err.context["event_type"] == "CLAIM_REQUESTED"

    def test_unregistered_node_progress_rejected(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """未注册节点的 PROGRESS_REPORTED 事件被拒绝。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        event_dict = _make_event(
            event_type="PROGRESS_REPORTED",
            node_id=_NODE_ID_A,
            event_id="evt-unknown-prog-0001",
            assignment_epoch=1,
        )
        _write_event_to_node_branch(git_repo, _NODE_ID_A, event_dict)

        event = CoordinationEventModel.model_validate(event_dict)
        with pytest.raises(NodeIdentityError) as exc_info:
            _run(service.verify_node_identity(event, expected_node_id=_NODE_ID_A))

        assert exc_info.value.reason_code == ReasonCode.EVENT_NODE_UNKNOWN.value


# --------------------------------------------------------------------------- #
# 验收 3：source 不匹配拒绝
# --------------------------------------------------------------------------- #


class TestSourceMismatchRejected:
    """event.node_id 与 expected_node_id 不一致 → 拒绝。"""

    def test_source_mismatch_rejected(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """事件 node_id 是 A，但 expected_node_id 是 B → 拒绝。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        # 注册节点 A 和 B。
        manifest_a = _make_manifest(node_id=_NODE_ID_A)
        manifest_b = _make_manifest(
            node_id=_NODE_ID_B,
            display_name="Test Node B",
        )
        _write_node_manifest_to_control(git_repo, _NODE_ID_A, manifest_a)
        _write_node_manifest_to_control(git_repo, _NODE_ID_B, manifest_b)

        # 节点 A 写入事件，但调用时传 expected_node_id=B（冒充 B）。
        event_dict = _make_event(
            event_type="CLAIM_REQUESTED",
            node_id=_NODE_ID_A,
            event_id="evt-impersonate-0001",
        )
        _write_event_to_node_branch(git_repo, _NODE_ID_A, event_dict)

        event = CoordinationEventModel.model_validate(event_dict)
        with pytest.raises(NodeIdentityError) as exc_info:
            _run(
                service.verify_node_identity(
                    event, expected_node_id=_NODE_ID_B
                )
            )

        err = exc_info.value
        assert err.reason_code == ReasonCode.EVENT_NODE_IDENTITY_MISMATCH.value
        assert err.context["failure_reason"] == "source_mismatch"
        assert err.context["event_node_id"] == _NODE_ID_A
        assert err.context["node_id"] == _NODE_ID_B


# --------------------------------------------------------------------------- #
# 验收 4：commit author 不匹配拒绝
# --------------------------------------------------------------------------- #


class TestCommitAuthorMismatchRejected:
    """commit author email 与 manifest 声明 email 不一致 → 拒绝。"""

    def test_commit_author_email_mismatch(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """事件 commit author email 与 manifest 声明不一致 → 拒绝。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        # 注册节点 A，声明 email = bot@example.test。
        manifest = _make_manifest(
            node_id=_NODE_ID_A,
            git_email=_DEFAULT_AUTHOR_EMAIL,
        )
        _write_node_manifest_to_control(git_repo, _NODE_ID_A, manifest)

        # 用攻击者身份提交事件到 maf/node/<A> 分支。
        event_dict = _make_event(
            event_type="CLAIM_REQUESTED",
            node_id=_NODE_ID_A,
            event_id="evt-attacker-aaa-0001",
        )
        _write_event_to_node_branch(
            git_repo, _NODE_ID_A, event_dict,
            author_name=_ATTACKER_AUTHOR_NAME,
            author_email=_ATTACKER_AUTHOR_EMAIL,
        )

        event = CoordinationEventModel.model_validate(event_dict)
        with pytest.raises(NodeIdentityError) as exc_info:
            _run(service.verify_node_identity(event, expected_node_id=_NODE_ID_A))

        err = exc_info.value
        assert err.reason_code == ReasonCode.EVENT_NODE_IDENTITY_MISMATCH.value
        assert err.context["failure_reason"] == "commit_author_mismatch"
        assert err.context["commit_author_email"] == _ATTACKER_AUTHOR_EMAIL
        assert err.context["declared_email"] == _DEFAULT_AUTHOR_EMAIL


# --------------------------------------------------------------------------- #
# 验收 5：自动注册禁止
# --------------------------------------------------------------------------- #


class TestNoAutoRegistration:
    """未知节点拒绝后不创建 nodes/<node-id>.yaml。"""

    def test_no_manifest_created_after_rejection(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """拒绝未知节点事件后，control 上不创建该节点的 manifest 文件。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        event_dict = _make_event(
            event_type="CLAIM_REQUESTED",
            node_id=_NODE_ID_A,
            event_id="evt-no-auto-reg-0001",
        )
        _write_event_to_node_branch(git_repo, _NODE_ID_A, event_dict)

        # 验证前 nodes/ 目录中无该节点 manifest。
        manifest_path = f".maf/nodes/{_NODE_ID_A}.yaml"
        assert not _control_file_exists(git_repo, manifest_path)

        event = CoordinationEventModel.model_validate(event_dict)
        with pytest.raises(NodeIdentityError):
            _run(service.verify_node_identity(event, expected_node_id=_NODE_ID_A))

        # 验证后仍不应创建 manifest（不自动注册）。
        assert not _control_file_exists(git_repo, manifest_path)

    def test_no_branch_created_for_unknown_node(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """verify_node_identity 不创建节点分支或 control 文件。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        # 节点 B 没有分支也没有 manifest。
        event_dict = _make_event(
            event_type="CLAIM_REQUESTED",
            node_id=_NODE_ID_B,
            event_id="evt-no-branch-0001",
        )
        event = CoordinationEventModel.model_validate(event_dict)

        # 由于节点分支不存在，_get_event_commit_author 会失败。
        with pytest.raises(NodeIdentityError) as exc_info:
            _run(service.verify_node_identity(event, expected_node_id=_NODE_ID_B))

        # 不论失败原因，control 上不应有 manifest。
        assert not _control_file_exists(git_repo, f".maf/nodes/{_NODE_ID_B}.yaml")


# --------------------------------------------------------------------------- #
# 验收 6：NODE_REGISTERED 事件使用 payload manifest
# --------------------------------------------------------------------------- #


class TestNodeRegisteredEventUsesPayloadManifest:
    """NODE_REGISTERED 事件使用 payload 中的 manifest（trust-on-first-use）。"""

    def test_node_registered_event_verified_via_payload(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """未注册节点的 NODE_REGISTERED 事件用 payload manifest 验证通过。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        # 构造 NODE_REGISTERED 事件，payload 含 manifest。
        manifest = _make_manifest(
            node_id=_NODE_ID_A,
            git_name=_DEFAULT_AUTHOR_NAME,
            git_email=_DEFAULT_AUTHOR_EMAIL,
        )
        event_dict = _make_node_registered_event(
            node_id=_NODE_ID_A,
            manifest=manifest,
            event_id="evt-register-aaa-0001",
        )
        # 用与 manifest 一致的 author 提交事件。
        _write_event_to_node_branch(
            git_repo, _NODE_ID_A, event_dict,
            author_name=_DEFAULT_AUTHOR_NAME,
            author_email=_DEFAULT_AUTHOR_EMAIL,
        )

        event = CoordinationEventModel.model_validate(event_dict)
        result = _run(
            service.verify_node_identity(event, expected_node_id=_NODE_ID_A)
        )

        assert result["verified"] is True
        assert result["manifest"] is not None
        assert result["manifest"]["node_id"] == _NODE_ID_A
        assert result["manifest"]["git_identity"]["email"] == _DEFAULT_AUTHOR_EMAIL

    def test_node_registered_event_author_mismatch_rejected(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """NODE_REGISTERED 事件 commit author 与 payload manifest 不一致 → 拒绝。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        manifest = _make_manifest(
            node_id=_NODE_ID_A,
            git_email=_DEFAULT_AUTHOR_EMAIL,
        )
        event_dict = _make_node_registered_event(
            node_id=_NODE_ID_A,
            manifest=manifest,
            event_id="evt-register-bad-0001",
        )
        # 用攻击者身份提交。
        _write_event_to_node_branch(
            git_repo, _NODE_ID_A, event_dict,
            author_name=_ATTACKER_AUTHOR_NAME,
            author_email=_ATTACKER_AUTHOR_EMAIL,
        )

        event = CoordinationEventModel.model_validate(event_dict)
        with pytest.raises(NodeIdentityError) as exc_info:
            _run(service.verify_node_identity(event, expected_node_id=_NODE_ID_A))

        assert exc_info.value.reason_code == ReasonCode.EVENT_NODE_IDENTITY_MISMATCH.value
        assert exc_info.value.context["failure_reason"] == "commit_author_mismatch"

    def test_node_registered_missing_manifest_in_payload_rejected(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """NODE_REGISTERED 事件 payload 无 manifest → 拒绝。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        event_dict = _make_event(
            event_type="NODE_REGISTERED",
            node_id=_NODE_ID_A,
            task_id=None,
            event_id="evt-register-no-manifest-0001",
            payload={"environment": {"hostname": "test"}},
        )
        _write_event_to_node_branch(git_repo, _NODE_ID_A, event_dict)

        event = CoordinationEventModel.model_validate(event_dict)
        with pytest.raises(NodeIdentityError) as exc_info:
            _run(service.verify_node_identity(event, expected_node_id=_NODE_ID_A))

        assert exc_info.value.reason_code == ReasonCode.EVENT_NODE_UNKNOWN.value
        assert exc_info.value.context["failure_reason"] == "missing_payload_manifest"

    def test_registered_node_uses_control_manifest_not_payload(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """已注册节点的 NODE_UPDATED 事件使用 control 上的 manifest 验证。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        # 注册节点 A，control 上声明 email = bot@example.test。
        control_manifest = _make_manifest(
            node_id=_NODE_ID_A,
            git_email=_DEFAULT_AUTHOR_EMAIL,
        )
        _write_node_manifest_to_control(git_repo, _NODE_ID_A, control_manifest)

        # NODE_UPDATED 事件 payload 中声明一个不同的 email（试图篡改身份）。
        payload_manifest = _make_manifest(
            node_id=_NODE_ID_A,
            git_email=_ATTACKER_AUTHOR_EMAIL,
        )
        event_dict = _make_event(
            event_type="NODE_UPDATED",
            node_id=_NODE_ID_A,
            task_id=None,
            event_id="evt-update-aaa-0001",
            payload={"manifest": payload_manifest, "environment": {}},
        )
        # 用 control 上声明的 author 提交（与 control manifest 一致）。
        _write_event_to_node_branch(
            git_repo, _NODE_ID_A, event_dict,
            author_name=_DEFAULT_AUTHOR_NAME,
            author_email=_DEFAULT_AUTHOR_EMAIL,
        )

        event = CoordinationEventModel.model_validate(event_dict)
        result = _run(
            service.verify_node_identity(event, expected_node_id=_NODE_ID_A)
        )

        # 验证通过：使用 control 上的 manifest（不是 payload 中的篡改版本）。
        assert result["verified"] is True
        assert result["manifest"]["git_identity"]["email"] == _DEFAULT_AUTHOR_EMAIL


# --------------------------------------------------------------------------- #
# 验收 7：空 expected_node_id 拒绝
# --------------------------------------------------------------------------- #


class TestEmptyExpectedNodeIdRejected:
    """空 expected_node_id 抛 ArgumentError。"""

    def test_empty_expected_node_id_raises_argument_error(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """空 expected_node_id 抛 ArgumentError（不是 NodeIdentityError）。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        event_dict = _make_event(event_id="evt-empty-expected-0001")
        event = CoordinationEventModel.model_validate(event_dict)

        with pytest.raises(ArgumentError, match="expected_node_id"):
            _run(service.verify_node_identity(event, expected_node_id=""))


# --------------------------------------------------------------------------- #
# 验收 8：manifest node_id 不匹配拒绝
# --------------------------------------------------------------------------- #


class TestManifestNodeIdMismatchRejected:
    """control 上的 manifest node_id 与 expected 不一致 → 拒绝。"""

    def test_manifest_node_id_mismatch(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """control 上 node-A 的 manifest 中 node_id 是 node-B → 拒绝。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        # 故意把 node_id=node-B 的 manifest 写到 nodes/node-A.yaml。
        bad_manifest = _make_manifest(
            node_id=_NODE_ID_B,
            display_name="Wrong Node",
        )
        _write_node_manifest_to_control(git_repo, _NODE_ID_A, bad_manifest)

        event_dict = _make_event(
            event_type="CLAIM_REQUESTED",
            node_id=_NODE_ID_A,
            event_id="evt-manifest-mismatch-0001",
        )
        _write_event_to_node_branch(git_repo, _NODE_ID_A, event_dict)

        event = CoordinationEventModel.model_validate(event_dict)
        with pytest.raises(NodeIdentityError) as exc_info:
            _run(service.verify_node_identity(event, expected_node_id=_NODE_ID_A))

        err = exc_info.value
        assert err.reason_code == ReasonCode.EVENT_NODE_IDENTITY_MISMATCH.value
        assert err.context["failure_reason"] == "manifest_node_id_mismatch"
        assert err.context["manifest_node_id"] == _NODE_ID_B


# --------------------------------------------------------------------------- #
# 只读语义：verify_node_identity 不修改 control 或 node 分支
# --------------------------------------------------------------------------- #


class TestReadOnlySemantics:
    """``verify_node_identity`` 不修改任何分支或工作树。"""

    def test_control_head_unchanged(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """调用前后 control 分支 HEAD 不变。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        manifest = _make_manifest(node_id=_NODE_ID_A)
        _write_node_manifest_to_control(git_repo, _NODE_ID_A, manifest)

        event_dict = _make_event(event_id="evt-readonly-aaa-0001")
        _write_event_to_node_branch(git_repo, _NODE_ID_A, event_dict)

        control_before = _git(git_repo, "rev-parse", "maf/control")
        node_before = _git(git_repo, "rev-parse", f"maf/node/{_NODE_ID_A}")

        event = CoordinationEventModel.model_validate(event_dict)
        _run(service.verify_node_identity(event, expected_node_id=_NODE_ID_A))

        control_after = _git(git_repo, "rev-parse", "maf/control")
        node_after = _git(git_repo, "rev-parse", f"maf/node/{_NODE_ID_A}")

        assert control_before == control_after
        assert node_before == node_after

    def test_main_worktree_unchanged(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """调用后 main 工作树仍在 main 分支。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        manifest = _make_manifest(node_id=_NODE_ID_A)
        _write_node_manifest_to_control(git_repo, _NODE_ID_A, manifest)

        event_dict = _make_event(event_id="evt-main-aaa-0001")
        _write_event_to_node_branch(git_repo, _NODE_ID_A, event_dict)

        event = CoordinationEventModel.model_validate(event_dict)
        _run(service.verify_node_identity(event, expected_node_id=_NODE_ID_A))

        current = _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD")
        assert current == "main"


# --------------------------------------------------------------------------- #
# 错误码与审计字段完整性
# --------------------------------------------------------------------------- #


class TestErrorAuditFields:
    """NodeIdentityError 携带完整审计字段（node_id、event_id、failure_reason）。"""

    def test_error_contains_node_id_and_event_id(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """错误 context 必含 node_id 和 event_id。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        event_dict = _make_event(
            event_type="CLAIM_REQUESTED",
            node_id=_NODE_ID_A,
            event_id="evt-audit-aaa-0001",
        )
        _write_event_to_node_branch(git_repo, _NODE_ID_A, event_dict)

        event = CoordinationEventModel.model_validate(event_dict)
        with pytest.raises(NodeIdentityError) as exc_info:
            _run(service.verify_node_identity(event, expected_node_id=_NODE_ID_A))

        err = exc_info.value
        assert err.error_code == ErrorCode.GIT_EVENT_REJECTED
        assert err.context["node_id"] == _NODE_ID_A
        assert err.context["event_id"] == "evt-audit-aaa-0001"
        assert "failure_reason" in err.context
        assert err.context["failure_reason"]  # non-empty

    def test_error_does_not_leak_credentials(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """错误 context 不含密码、token 等凭据字段。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        manifest = _make_manifest(node_id=_NODE_ID_A)
        _write_node_manifest_to_control(git_repo, _NODE_ID_A, manifest)

        event_dict = _make_event(
            event_type="CLAIM_REQUESTED",
            node_id=_NODE_ID_A,
            event_id="evt-no-leak-aaa-0001",
        )
        _write_event_to_node_branch(
            git_repo, _NODE_ID_A, event_dict,
            author_name=_ATTACKER_AUTHOR_NAME,
            author_email=_ATTACKER_AUTHOR_EMAIL,
        )

        event = CoordinationEventModel.model_validate(event_dict)
        with pytest.raises(NodeIdentityError) as exc_info:
            _run(service.verify_node_identity(event, expected_node_id=_NODE_ID_A))

        context_keys = set(exc_info.value.context.keys())
        # 不含密码、token、secret 等凭据字段。
        forbidden_patterns = ("password", "token", "secret", "credential", "key")
        for key in context_keys:
            for pattern in forbidden_patterns:
                assert pattern not in key.lower(), (
                    f"context key {key!r} contains forbidden pattern {pattern!r}"
                )
