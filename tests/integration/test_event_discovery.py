"""TASK-019 集成测试：发现节点事件。

验收标准覆盖（对应 TASK-019 文档）：

1. **多节点事件都能发现**：``discover_all_node_events`` 枚举所有
   ``maf/node/*`` 分支并返回每个节点的事件；``discover_node_events``
   返回单个节点分支上的全部事件。
2. **强推、历史回退和损坏事件分支被隔离**：``since_commit`` 不是 HEAD
   祖先时 ``diverged=True``；非法 JSON / Schema 校验失败的事件文件进入
   ``invalid_events``，不静默丢弃，不阻断其他合法事件的发现。
3. **扫描不依赖事件机器时间排序**：返回的 ``events`` 按 ``event_id`` 字典序
   排序，与 ``occurred_at`` 机器时间无关。

附加测试：
- 增量发现（``since_commit`` 只返回新增事件）。
- 全量发现（``since_commit=None`` 返回全部事件）。
- ``since_commit == head`` 快速返回空列表。
- 只读语义：调用前后分支 HEAD 与工作树不变。
- 分支不存在时返回 ``branch_exists=False``。

测试使用 ``tests/fixtures/git_repo.py`` 的 ``init_local_git_repo`` 创建真实
git 仓库，用 TASK-015 的 ``initialize_project`` 初始化 control 分支，再通过
worktree 直接在 ``maf/node/<node-id>`` 分支写入事件文件（与协议 §9 的
append-only 语义一致）。所有异步入口经 ``asyncio.run`` 同步执行。
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

# packages/artifact_schemas/src 尚未加入 pyproject.toml pythonpath（TASK-002 范围），
# 此处显式添加，使 maf_artifact_schemas 可被 maf_server.git_coordination.schemas
# 与 maf_server.modules.git_coordination.service 导入。与现有
# tests/contract/test_control_reader.py 一致。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT_SCHEMAS_SRC = _PROJECT_ROOT / "packages" / "artifact_schemas" / "src"
if str(_ARTIFACT_SCHEMAS_SRC) not in sys.path:
    sys.path.insert(0, str(_ARTIFACT_SCHEMAS_SRC))

from maf_domain.errors import ArgumentError  # noqa: E402
from maf_repository_adapters import SubprocessGitCli  # noqa: E402
from maf_server.git_coordination.schemas import SchemaLoader  # noqa: E402
from maf_server.modules.git_coordination.service import (  # noqa: E402
    DiscoveredEvents,
    LocalGitCoordinationService,
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

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test Bot",
    "GIT_AUTHOR_EMAIL": "bot@example.test",
    "GIT_COMMITTER_NAME": "Test Bot",
    "GIT_COMMITTER_EMAIL": "bot@example.test",
    "GIT_TERMINAL_PROMPT": "0",
}


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #


def _run(coro: Any) -> Any:
    """在独立事件循环中执行协程并返回结果（与现有集成测试风格一致）。"""
    return asyncio.run(coro)


def _git(repo: Path, *args: str) -> str:
    """同步执行 git 命令，返回 stdout（用于 fixture 准备与断言）。

    直接用 ``subprocess.run`` 而非 GitCli：测试 fixture 不需要白名单/路径限制
    等安全保证。
    """
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=_GIT_ENV,
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


def _write_event_to_node_branch(
    repo: Path,
    node_id: str,
    event: dict[str, Any],
    *,
    content: str | None = None,
) -> str:
    """Write an event file to ``maf/node/<node-id>`` branch, return new HEAD.

    Uses a temporary worktree to avoid disturbing the current branch. Creates
    an orphan branch on first write (matching the protocol: node branches do
    not share main's history). Appends to existing branch on subsequent writes.

    Parameters:
        repo: path to the main repository.
        node_id: the node whose branch to write to.
        event: the event dict (used for file name and default content).
        content: override file content (for writing invalid JSON / bad schema).
    """
    branch = f"maf/node/{node_id}"
    event_id = event["event_id"]
    rel_path = f".maf/events/{event_id}.json"
    wt = repo / ".maf-event-wt"

    # Clean up any stale worktree from a previous test/failed assertion.
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
        env=_GIT_ENV,
        capture_output=True,
    )

    branch_exists = subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
         f"refs/heads/{branch}"],
        env=_GIT_ENV,
    ).returncode == 0

    if not branch_exists:
        # Orphan branch: no parent commit, empty working tree.
        _git(repo, "worktree", "add", "--orphan", "-b", branch, str(wt))
    else:
        # Check out existing branch into a new worktree for append.
        _git(repo, "worktree", "add", str(wt), branch)

    event_path = wt / rel_path
    event_path.parent.mkdir(parents=True, exist_ok=True)
    file_content = content if content is not None else (
        json.dumps(event, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    )
    event_path.write_text(file_content, encoding="utf-8")

    _git(wt, "add", "--", rel_path)
    _git(wt, "commit", "-q", "-m", f"event: {event_id}")
    new_head = _git(wt, "rev-parse", "HEAD")

    # Remove the temporary worktree; the branch ref persists.
    _git(repo, "worktree", "remove", "--force", str(wt))
    return new_head


def _reset_node_branch_to_orphan(
    repo: Path,
    node_id: str,
    event: dict[str, Any],
) -> str:
    """Reset ``maf/node/<node-id>`` to a NEW orphan commit (simulate force-push).

    Deletes the existing branch and creates a fresh orphan branch with the
    given event. The new HEAD has no ancestry relationship with the old HEAD,
    so ``since_commit=<old-head>`` will be detected as ``diverged``.
    """
    branch = f"maf/node/{node_id}"
    # Delete the existing branch (safe: not checked out after worktree cleanup).
    _git(repo, "branch", "-D", branch)
    # Re-create as orphan with the new event.
    return _write_event_to_node_branch(repo, node_id, event)


def _branch_head(repo: Path, branch: str) -> str:
    """Return the current HEAD commit of ``branch``."""
    return _git(repo, "rev-parse", branch)


def _branch_exists(repo: Path, branch: str) -> bool:
    """Check whether ``branch`` exists in ``repo``."""
    rc = subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet",
         f"refs/heads/{branch}"],
        env=_GIT_ENV,
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
    """使用 ``init_local_git_repo`` 初始化一个真实 git 仓库（含 main 与初始提交）。"""
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
# 验收 1：多节点事件都能发现
# --------------------------------------------------------------------------- #


class TestDiscoverSingleNodeEvents:
    """``discover_node_events`` 返回单个节点分支上的全部事件。"""

    def test_single_event_discovered(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """单个事件写入后被发现，字段完整。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        event = _make_event(event_type="NODE_REGISTERED", task_id=None)
        _write_event_to_node_branch(git_repo, _NODE_ID_A, event)

        result: DiscoveredEvents = _run(
            service.discover_node_events("proj-test-001", _NODE_ID_A)
        )

        assert result["branch_exists"] is True
        assert result["branch"] == f"maf/node/{_NODE_ID_A}"
        assert result["latest_commit"] is not None
        assert len(result["events"]) == 1
        assert result["events"][0]["event_id"] == event["event_id"]
        assert result["events"][0]["event_type"] == "NODE_REGISTERED"
        assert result["events"][0]["node_id"] == _NODE_ID_A
        assert result["invalid_events"] == []
        assert result["diverged"] is False

    def test_multiple_events_discovered(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """连续追加两个事件，都被发现。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        event1 = _make_event(event_id="evt-aaaaaaaa-0001", event_type="CLAIM_REQUESTED")
        event2 = _make_event(event_id="evt-bbbbbbbb-0002", event_type="PROGRESS_REPORTED",
                             assignment_epoch=1)
        _write_event_to_node_branch(git_repo, _NODE_ID_A, event1)
        _write_event_to_node_branch(git_repo, _NODE_ID_A, event2)

        result = _run(service.discover_node_events("proj-test-001", _NODE_ID_A))

        assert len(result["events"]) == 2
        ids = {e["event_id"] for e in result["events"]}
        assert ids == {"evt-aaaaaaaa-0001", "evt-bbbbbbbb-0002"}

    def test_branch_not_exists_returns_empty(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """节点分支不存在时返回 branch_exists=False。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        result = _run(service.discover_node_events("proj-test-001", _NODE_ID_A))

        assert result["branch_exists"] is False
        assert result["latest_commit"] is None
        assert result["events"] == []
        assert result["invalid_events"] == []
        assert result["diverged"] is False

    def test_empty_node_id_rejected(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """空 node_id 抛 ArgumentError。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        with pytest.raises(ArgumentError, match="node_id"):
            _run(service.discover_node_events("proj-test-001", ""))

    def test_empty_project_id_rejected(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """空 project_id 抛 ArgumentError。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        with pytest.raises(ArgumentError, match="project_id"):
            _run(service.discover_node_events("", _NODE_ID_A))


class TestDiscoverAllNodeEvents:
    """``discover_all_node_events`` 枚举所有 ``maf/node/*`` 分支。"""

    def test_multiple_nodes_discovered(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """两个节点分支上的事件都被发现。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        event_a = _make_event(node_id=_NODE_ID_A, event_id="evt-node-aaaa-0001")
        event_b = _make_event(node_id=_NODE_ID_B, event_id="evt-node-bbbb-0001")
        _write_event_to_node_branch(git_repo, _NODE_ID_A, event_a)
        _write_event_to_node_branch(git_repo, _NODE_ID_B, event_b)

        results = _run(service.discover_all_node_events("proj-test-001"))

        assert _NODE_ID_A in results
        assert _NODE_ID_B in results
        assert len(results[_NODE_ID_A]["events"]) == 1
        assert len(results[_NODE_ID_B]["events"]) == 1
        assert results[_NODE_ID_A]["events"][0]["event_id"] == "evt-node-aaaa-0001"
        assert results[_NODE_ID_B]["events"][0]["event_id"] == "evt-node-bbbb-0001"

    def test_no_node_branches_returns_empty_dict(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """无节点分支时返回空 dict。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        results = _run(service.discover_all_node_events("proj-test-001"))
        assert results == {}

    def test_non_node_branches_skipped(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``maf/control`` 等非 ``maf/node/*`` 分支不被枚举。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)
        # maf/control 已存在；确保 discover_all 不把它当节点分支。
        event_a = _make_event(node_id=_NODE_ID_A, event_id="evt-only-aaa-0001")
        _write_event_to_node_branch(git_repo, _NODE_ID_A, event_a)

        results = _run(service.discover_all_node_events("proj-test-001"))

        assert _NODE_ID_A in results
        # ``control`` 不是 ``maf/node/`` 前缀，不应出现在结果中。
        assert "control" not in results
        assert len(results) == 1


# --------------------------------------------------------------------------- #
# 验收 2：强推、历史回退和损坏事件分支被隔离
# --------------------------------------------------------------------------- #


class TestDivergedDetection:
    """``since_commit`` 不是 HEAD 祖先时 ``diverged=True``。"""

    def test_diverged_on_force_push(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """强推（分支重置为无关 orphan commit）后 diverged=True。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        event1 = _make_event(event_id="evt-oldoldol-00001")
        old_head = _write_event_to_node_branch(git_repo, _NODE_ID_A, event1)

        # 模拟强推：删除分支并重建为无关 orphan commit。
        event2 = _make_event(event_id="evt-newnewne-00002")
        _reset_node_branch_to_orphan(git_repo, _NODE_ID_A, event2)

        result = _run(
            service.discover_node_events(
                "proj-test-001", _NODE_ID_A, since_commit=old_head
            )
        )

        assert result["diverged"] is True
        assert result["branch_exists"] is True
        # 回退全量扫描：只发现当前分支上的 event2。
        ids = {e["event_id"] for e in result["events"]}
        assert "evt-newnewne-00002" in ids

    def test_diverged_on_unknown_since_commit(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``since_commit`` 不存在时视为 diverged（回退全量扫描）。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        event = _make_event(event_id="evt-unknown-00001")
        _write_event_to_node_branch(git_repo, _NODE_ID_A, event)

        fake_commit = "0" * 40
        result = _run(
            service.discover_node_events(
                "proj-test-001", _NODE_ID_A, since_commit=fake_commit
            )
        )

        assert result["diverged"] is True
        assert len(result["events"]) == 1


class TestInvalidEventReporting:
    """非法事件文件进入 ``invalid_events``，不阻断合法事件发现。"""

    def test_invalid_json_reported(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """非 JSON 文件进入 invalid_events。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        bad_event = _make_event(event_id="evt-bad-json-001")
        _write_event_to_node_branch(
            git_repo, _NODE_ID_A, bad_event, content="{not valid json"
        )

        result = _run(service.discover_node_events("proj-test-001", _NODE_ID_A))

        assert len(result["invalid_events"]) == 1
        assert result["invalid_events"][0]["path"].endswith("evt-bad-json-001.json")
        assert "invalid_json" in result["invalid_events"][0]["error"]
        assert result["invalid_events"][0]["raw_content"] is not None
        assert result["events"] == []

    def test_schema_validation_failure_reported(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """JSON 合法但 Schema 校验失败的事件进入 invalid_events。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        # 缺少必填字段 event_id。
        bad_event = _make_event(event_id="evt-schema-001")
        bad_content = json.dumps({**bad_event, "event_id": "x"})  # event_id 太短
        _write_event_to_node_branch(
            git_repo, _NODE_ID_A, bad_event, content=bad_content
        )

        result = _run(service.discover_node_events("proj-test-001", _NODE_ID_A))

        assert len(result["invalid_events"]) == 1
        assert "schema_validation_failed" in result["invalid_events"][0]["error"]
        assert result["events"] == []

    def test_extra_field_rejected(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``extra="forbid"`` 模型拒绝未知字段。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        event = _make_event(event_id="evt-extra-001")
        bad_content = json.dumps({**event, "unknown_field": "bad"})
        _write_event_to_node_branch(
            git_repo, _NODE_ID_A, event, content=bad_content
        )

        result = _run(service.discover_node_events("proj-test-001", _NODE_ID_A))

        assert len(result["invalid_events"]) == 1
        assert result["events"] == []

    def test_mixed_valid_and_invalid(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """合法与非法事件混合时，合法事件返回，非法事件报告。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        good_event = _make_event(event_id="evt-goodgood-00001")
        bad_event = _make_event(event_id="evt-badbad-00002")
        _write_event_to_node_branch(git_repo, _NODE_ID_A, good_event)
        _write_event_to_node_branch(
            git_repo, _NODE_ID_A, bad_event, content="not json at all"
        )

        result = _run(service.discover_node_events("proj-test-001", _NODE_ID_A))

        assert len(result["events"]) == 1
        assert result["events"][0]["event_id"] == "evt-goodgood-00001"
        assert len(result["invalid_events"]) == 1
        assert result["invalid_events"][0]["path"].endswith("evt-badbad-00002.json")


# --------------------------------------------------------------------------- #
# 验收 3：扫描不依赖事件机器时间排序
# --------------------------------------------------------------------------- #


class TestDeterministicSortByEventId:
    """事件按 ``event_id`` 排序，与 ``occurred_at`` 机器时间无关。"""

    def test_sorted_by_event_id_not_occurred_at(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``occurred_at`` 逆序写入但 ``event_id`` 字典序排列。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        # event_id 字典序：evt-000000000001 < evt-000000000002 < evt-000000000003
        # occurred_at 逆序：evt-000000000001 最新，evt-000000000003 最旧
        e1 = _make_event(event_id="evt-000000000001", occurred_at="2026-07-17T03:00:00Z")
        e2 = _make_event(event_id="evt-000000000002", occurred_at="2026-07-17T02:00:00Z")
        e3 = _make_event(event_id="evt-000000000003", occurred_at="2026-07-17T01:00:00Z")
        _write_event_to_node_branch(git_repo, _NODE_ID_A, e1)
        _write_event_to_node_branch(git_repo, _NODE_ID_A, e2)
        _write_event_to_node_branch(git_repo, _NODE_ID_A, e3)

        result = _run(service.discover_node_events("proj-test-001", _NODE_ID_A))

        ids = [e["event_id"] for e in result["events"]]
        assert ids == ["evt-000000000001", "evt-000000000002", "evt-000000000003"]

    def test_invalid_events_sorted_by_path(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``invalid_events`` 按文件路径排序（确定性）。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        # 逆序写入，确保排序由 discover 逻辑完成而非写入顺序。
        bad3 = _make_event(event_id="evt-zzz-003")
        bad1 = _make_event(event_id="evt-aaaaaaaa-0001")
        bad2 = _make_event(event_id="evt-mmm-002")
        _write_event_to_node_branch(git_repo, _NODE_ID_A, bad3, content="bad")
        _write_event_to_node_branch(git_repo, _NODE_ID_A, bad1, content="bad")
        _write_event_to_node_branch(git_repo, _NODE_ID_A, bad2, content="bad")

        result = _run(service.discover_node_events("proj-test-001", _NODE_ID_A))

        paths = [i["path"] for i in result["invalid_events"]]
        assert paths == sorted(paths)
        assert paths[0].endswith("evt-aaaaaaaa-0001.json")
        assert paths[1].endswith("evt-mmm-002.json")
        assert paths[2].endswith("evt-zzz-003.json")


# --------------------------------------------------------------------------- #
# 增量发现与全量发现
# --------------------------------------------------------------------------- #


class TestIncrementalDiscovery:
    """``since_commit`` 增量发现只返回新增事件。"""

    def test_full_scan_when_since_commit_none(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``since_commit=None`` 返回全部事件。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        e1 = _make_event(event_id="evt-fullfull-0001")
        e2 = _make_event(event_id="evt-fullfull-0002")
        e3 = _make_event(event_id="evt-fullfull-0003")
        _write_event_to_node_branch(git_repo, _NODE_ID_A, e1)
        _write_event_to_node_branch(git_repo, _NODE_ID_A, e2)
        _write_event_to_node_branch(git_repo, _NODE_ID_A, e3)

        result = _run(service.discover_node_events("proj-test-001", _NODE_ID_A))

        assert len(result["events"]) == 3
        assert result["diverged"] is False

    def test_incremental_returns_only_new_events(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``since_commit=<old-head>`` 只返回新增事件。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        e1 = _make_event(event_id="evt-incrincr-0001")
        e2 = _make_event(event_id="evt-incrincr-0002")
        _write_event_to_node_branch(git_repo, _NODE_ID_A, e1)
        watermark = _write_event_to_node_branch(git_repo, _NODE_ID_A, e2)

        e3 = _make_event(event_id="evt-incrincr-0003")
        e4 = _make_event(event_id="evt-incrincr-0004")
        _write_event_to_node_branch(git_repo, _NODE_ID_A, e3)
        _write_event_to_node_branch(git_repo, _NODE_ID_A, e4)

        result = _run(
            service.discover_node_events(
                "proj-test-001", _NODE_ID_A, since_commit=watermark
            )
        )

        assert result["diverged"] is False
        ids = {e["event_id"] for e in result["events"]}
        assert ids == {"evt-incrincr-0003", "evt-incrincr-0004"}
        # latest_commit 是当前 HEAD（包含 e4）。
        assert result["latest_commit"] == _branch_head(
            git_repo, f"maf/node/{_NODE_ID_A}"
        )

    def test_since_commit_equals_head_returns_empty(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``since_commit == head`` 快速返回空列表。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        e1 = _make_event(event_id="evt-noopnoop-0001")
        head = _write_event_to_node_branch(git_repo, _NODE_ID_A, e1)

        result = _run(
            service.discover_node_events(
                "proj-test-001", _NODE_ID_A, since_commit=head
            )
        )

        assert result["events"] == []
        assert result["diverged"] is False
        assert result["latest_commit"] == head
        assert result["scanned_paths"] == []


# --------------------------------------------------------------------------- #
# 只读语义
# --------------------------------------------------------------------------- #


class TestReadOnlySemantics:
    """``discover_node_events`` 不修改分支、不创建 commit、不切换工作树。"""

    def test_branch_head_unchanged(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """调用前后节点分支 HEAD 不变。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        _write_event_to_node_branch(git_repo, _NODE_ID_A, _make_event())
        branch = f"maf/node/{_NODE_ID_A}"
        head_before = _branch_head(git_repo, branch)

        _run(service.discover_node_events("proj-test-001", _NODE_ID_A))

        head_after = _branch_head(git_repo, branch)
        assert head_before == head_after

    def test_no_new_commits_created(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """调用不创建新 commit（commit 数不变）。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        _write_event_to_node_branch(git_repo, _NODE_ID_A, _make_event())
        branch = f"maf/node/{_NODE_ID_A}"
        count_before = int(_git(git_repo, "rev-list", "--count", branch))

        _run(service.discover_node_events("proj-test-001", _NODE_ID_A))
        _run(service.discover_all_node_events("proj-test-001"))

        count_after = int(_git(git_repo, "rev-list", "--count", branch))
        assert count_before == count_after

    def test_main_worktree_unchanged(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """调用后 main 工作树仍在 main 分支，不切换到节点分支。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        _write_event_to_node_branch(git_repo, _NODE_ID_A, _make_event())

        _run(service.discover_node_events("proj-test-001", _NODE_ID_A))

        current = _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD")
        assert current == "main"

    def test_no_branch_created_for_missing_node(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """分支不存在时调用 discover 不会创建分支。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        branch = f"maf/node/{_NODE_ID_A}"
        assert not _branch_exists(git_repo, branch)

        _run(service.discover_node_events("proj-test-001", _NODE_ID_A))

        assert not _branch_exists(git_repo, branch)


# --------------------------------------------------------------------------- #
# scanned_paths 完整性
# --------------------------------------------------------------------------- #


class TestScannedPaths:
    """``scanned_paths`` 列出所有被读取的 ``.json`` 文件路径。"""

    def test_scanned_paths_includes_all_json_files(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """全量扫描时 scanned_paths 包含所有 .json 事件文件。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        e1 = _make_event(event_id="evt-scanscn-0001")
        e2 = _make_event(event_id="evt-scanscn-0002")
        _write_event_to_node_branch(git_repo, _NODE_ID_A, e1)
        _write_event_to_node_branch(git_repo, _NODE_ID_A, e2)

        result = _run(service.discover_node_events("proj-test-001", _NODE_ID_A))

        expected = {
            ".maf/events/evt-scanscn-0001.json",
            ".maf/events/evt-scanscn-0002.json",
        }
        assert set(result["scanned_paths"]) == expected

    def test_non_json_files_skipped(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """非 ``.json`` 文件不出现在 scanned_paths / events / invalid_events。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service)

        # Write a valid event.
        event = _make_event(event_id="evt-only-json-001")
        _write_event_to_node_branch(git_repo, _NODE_ID_A, event)

        # Write a non-.json file on the same branch.
        branch = f"maf/node/{_NODE_ID_A}"
        wt = git_repo / ".maf-event-wt"
        subprocess.run(
            ["git", "-C", str(git_repo), "worktree", "remove", "--force", str(wt)],
            env=_GIT_ENV, capture_output=True,
        )
        _git(git_repo, "worktree", "add", str(wt), branch)
        readme = wt / ".maf" / "README.txt"
        readme.parent.mkdir(parents=True, exist_ok=True)
        readme.write_text("not an event\n", encoding="utf-8")
        _git(wt, "add", "--", ".maf/README.txt")
        _git(wt, "commit", "-q", "-m", "add non-json file")
        _git(git_repo, "worktree", "remove", "--force", str(wt))

        result = _run(service.discover_node_events("proj-test-001", _NODE_ID_A))

        assert len(result["events"]) == 1
        assert all(p.endswith(".json") for p in result["scanned_paths"])
        assert not any("README.txt" in p for p in result["scanned_paths"])
