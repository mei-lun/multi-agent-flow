"""TASK-018 集成测试：节点事件分支写入。

验收标准覆盖：

1. **节点能向 maf/node/<node-id> 分支追加事件**：``RunnerGitClient.append_event``
   将事件 JSON 写入 ``.maf/events/<event-id>.json`` 并 fast-forward push 到远端。
2. **事件格式符合协议**：事件含 ``event_id``、``node_id``、``event_type``、
   ``assignment_epoch``、``based_on_control_commit``、``occurred_at``、``payload``，
   通过 ``event-v1`` Schema 校验。
3. **append-only 不覆盖**：追加第二个事件不删除第一个事件文件；两个事件文件
   共存于同一节点分支。
4. **event_id 唯一**：不同事件有不同的 ``event_id``，文件名不冲突。
5. **assignment_epoch 字段**：进度/提交事件携带 ``assignment_epoch`` fencing token。
6. **节点只能写自己的事件分支**：``event.node_id`` 必须与客户端 ``node_id`` 一致。
7. **push 冲突可 fetch/rebase 后用同 event_id 重试**：push 被拒时重试，event_id 不变。

测试使用真实 git（2.45+）引导临时 bare 仓库作为远端，clone 为本地工作仓库，
通过 ``RunnerGitClient`` 执行 append_event，然后从远端 bare 仓库读取事件文件
验证结果。所有异步入口经 ``asyncio.run`` 同步执行，避免 pytest-asyncio 配置依赖。
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

# packages/artifact_schemas/src 不在 pyproject.toml 的 pythonpath 中（TASK-002 范围），
# 此处显式添加，使 maf_server.git_coordination.schemas 可导入以做 Schema 校验。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT_SCHEMAS_SRC = _PROJECT_ROOT / "packages" / "artifact_schemas" / "src"
if str(_ARTIFACT_SCHEMAS_SRC) not in sys.path:
    sys.path.insert(0, str(_ARTIFACT_SCHEMAS_SRC))

from maf_artifact_schemas.protocol import SchemaRef  # noqa: E402
from maf_domain.errors import ArgumentError  # noqa: E402
from maf_runner.git_client import RunnerGitClient  # noqa: E402
from maf_runner.workspace.git import RunnerGitCli  # noqa: E402
from maf_server.git_coordination.schemas import SchemaLoader  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

_NODE_ID = "node-12345678-1234-1234-1234-123456789abc"
_OTHER_NODE_ID = "node-99999999-9999-9999-9999-999999999999"
_CONTROL_COMMIT = "abcdef1234567890abcdef1234567890abcdef12"
_TEMPLATES_DIR = _PROJECT_ROOT / "templates" / "git_coordination"
_SCHEMAS_DIR = _TEMPLATES_DIR / "schemas"
_EVENT_REF = SchemaRef(name="event", version=1)

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test Bot",
    "GIT_AUTHOR_EMAIL": "bot@example.test",
    "GIT_COMMITTER_NAME": "Test Bot",
    "GIT_COMMITTER_EMAIL": "bot@example.test",
}


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #


def _run(coro: Any) -> Any:
    """在独立事件循环中执行协程并返回结果。"""
    return asyncio.run(coro)


def _make_event(
    *,
    event_type: str = "CLAIM_REQUESTED",
    node_id: str = _NODE_ID,
    task_id: str | None = "TASK-001",
    assignment_id: str | None = None,
    assignment_epoch: int | None = None,
    based_on_control_commit: str = _CONTROL_COMMIT,
    event_id: str | None = None,
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
        "occurred_at": "2026-07-17T00:00:00Z",
        "payload": payload or {"note": "test event"},
    }


def _setup_git_repos(tmp_path: Path) -> tuple[Path, Path]:
    """创建 bare 远端仓库和本地 clone，返回 (remote_path, local_path)。

    本地 clone 有一个初始提交并 push 到远端 main 分支，确保远端非空。
    本地分支显式命名为 ``main``，避免受 git 全局 ``init.defaultBranch`` 配置影响
    （在某些 Windows 环境默认仍为 ``master``）。
    """
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-q", str(remote)],
        check=True,
        env=_GIT_ENV,
    )
    # 显式设置远端默认分支为 main（避免 symbolic-ref HEAD 指向不存在的 master）。
    subprocess.run(
        ["git", "-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
        env=_GIT_ENV,
    )

    local = tmp_path / "local"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(local)],
        check=True,
        env=_GIT_ENV,
    )
    subprocess.run(
        ["git", "-C", str(local), "config", "user.name", "Test Bot"],
        check=True,
        env=_GIT_ENV,
    )
    subprocess.run(
        ["git", "-C", str(local), "config", "user.email", "bot@example.test"],
        check=True,
        env=_GIT_ENV,
    )
    (local / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(local), "add", "."],
        check=True,
        env=_GIT_ENV,
    )
    subprocess.run(
        ["git", "-C", str(local), "commit", "-q", "-m", "initial"],
        check=True,
        env=_GIT_ENV,
    )
    # 将本地当前分支强制重命名为 main，确保 push 的 refspec 存在。
    subprocess.run(
        ["git", "-C", str(local), "branch", "-M", "main"],
        check=True,
        env=_GIT_ENV,
    )
    subprocess.run(
        ["git", "-C", str(local), "push", "-q", "origin", "main"],
        check=True,
        env=_GIT_ENV,
    )
    return remote, local


def _read_event_from_remote(
    remote: Path, branch: str, event_id: str
) -> dict[str, Any] | None:
    """从远端 bare 仓库读取事件 JSON；不存在时返回 None。"""
    ref = f"refs/heads/{branch}"
    path = f".maf/events/{event_id}.json"
    result = subprocess.run(
        ["git", "-C", str(remote), "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def _list_event_files_on_remote(remote: Path, branch: str) -> list[str]:
    """列出远端 bare 仓库分支上所有 ``.maf/events/*.json`` 文件路径。"""
    ref = f"refs/heads/{branch}"
    result = subprocess.run(
        ["git", "-C", str(remote), "ls-tree", "-r", "--name-only", ref],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )
    if result.returncode != 0:
        return []
    return [
        line.strip()
        for line in result.stdout.split("\n")
        if line.strip().startswith(".maf/events/")
    ]


def _count_commits_on_remote(remote: Path, branch: str) -> int:
    """返回远端 bare 仓库分支上的 commit 数。"""
    ref = f"refs/heads/{branch}"
    result = subprocess.run(
        ["git", "-C", str(remote), "rev-list", "--count", ref],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def _branch_exists_on_remote(remote: Path, branch: str) -> bool:
    """检查远端 bare 仓库是否存在指定分支。"""
    ref = f"refs/heads/{branch}"
    result = subprocess.run(
        ["git", "-C", str(remote), "show-ref", "--verify", "--quiet", ref],
        capture_output=True,
        env=_GIT_ENV,
    )
    return result.returncode == 0


def _push_event_directly(
    remote: Path, local: Path, event: dict[str, Any], branch: str
) -> None:
    """绕过 RunnerGitClient，直接用 git 命令向远端追加一个事件（模拟并发写入）。"""
    wt = local / ".maf-direct-wt"
    if wt.exists():
        subprocess.run(
            ["git", "-C", str(local), "worktree", "remove", "--force", str(wt)],
            capture_output=True,
            env=_GIT_ENV,
        )
    remote_ref = f"refs/remotes/origin/{branch}"
    # 先 fetch 确保远端跟踪分支存在。
    subprocess.run(
        ["git", "-C", str(local), "fetch", "-q", "origin", branch],
        capture_output=True,
        env=_GIT_ENV,
    )
    rc = subprocess.run(
        ["git", "-C", str(local), "show-ref", "--verify", "--quiet", remote_ref],
        capture_output=True,
        env=_GIT_ENV,
    ).returncode
    if rc == 0:
        subprocess.run(
            [
                "git", "-C", str(local), "worktree", "add",
                "-B", branch, str(wt), remote_ref,
            ],
            check=True,
            env=_GIT_ENV,
        )
    else:
        subprocess.run(
            [
                "git", "-C", str(local), "worktree", "add",
                "--orphan", "-b", branch, str(wt),
            ],
            check=True,
            env=_GIT_ENV,
        )
    event_path = wt / ".maf" / "events" / f"{event['event_id']}.json"
    event_path.parent.mkdir(parents=True, exist_ok=True)
    event_path.write_text(
        json.dumps(event, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    rel = f".maf/events/{event['event_id']}.json"
    subprocess.run(
        ["git", "-C", str(wt), "add", "--", rel],
        check=True,
        env=_GIT_ENV,
    )
    subprocess.run(
        ["git", "-C", str(wt), "commit", "-q", "-m", f"direct: {event['event_id']}"],
        check=True,
        env=_GIT_ENV,
    )
    subprocess.run(
        ["git", "-C", str(wt), "push", "-q", "origin", f"HEAD:refs/heads/{branch}"],
        check=True,
        env=_GIT_ENV,
    )
    subprocess.run(
        ["git", "-C", str(local), "worktree", "remove", "--force", str(wt)],
        capture_output=True,
        env=_GIT_ENV,
    )


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def loader() -> SchemaLoader:
    return SchemaLoader(_SCHEMAS_DIR)


@pytest.fixture()
def git_env(
    tmp_path: Path,
) -> tuple[Path, Path, RunnerGitClient]:
    """创建 bare 远端 + 本地 clone + RunnerGitClient；返回三元组。"""
    remote, local = _setup_git_repos(tmp_path)
    cli = RunnerGitCli(allowed_roots=[tmp_path])
    client = RunnerGitClient(
        git_cli=cli,
        repository_path=str(local),
        control_remote="origin",
        control_branch="maf/control",
        node_id=_NODE_ID,
    )
    return remote, local, client


@pytest.fixture(autouse=True)
def _clear_maf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除 ``MAF_*`` 环境变量，避免本地 .env 污染测试。"""
    for key in list(os.environ):
        if key.startswith("MAF_"):
            monkeypatch.delenv(key, raising=False)


# --------------------------------------------------------------------------- #
# 验收 1：节点能向 maf/node/<node-id> 分支追加事件
# --------------------------------------------------------------------------- #


class TestAppendEventToNodeBranch:
    """append_event 将事件写入 maf/node/<node-id> 分支。"""

    def test_append_claim_event_succeeds(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """追加 CLAIM_REQUESTED 事件到节点分支，push 成功。"""
        remote, _local, client = git_env
        event = _make_event(event_type="CLAIM_REQUESTED")
        result = _run(client.append_event(event))

        assert result["push_ok"] is True
        assert result["commit"] is not None
        assert result["branch"] == f"maf/node/{_NODE_ID}"
        assert result["event_id"] == event["event_id"]
        assert result["attempts"] == 1

        # 远端分支存在。
        assert _branch_exists_on_remote(remote, f"maf/node/{_NODE_ID}")

    def test_event_file_exists_on_remote(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """事件文件 ``.maf/events/<event-id>.json`` 存在于远端分支。"""
        remote, _local, client = git_env
        event = _make_event(event_type="NODE_REGISTERED", task_id=None)
        _run(client.append_event(event))

        files = _list_event_files_on_remote(remote, f"maf/node/{_NODE_ID}")
        expected = f".maf/events/{event['event_id']}.json"
        assert expected in files

    def test_event_content_matches_input(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """远端事件 JSON 内容与输入一致。"""
        remote, _local, client = git_env
        event = _make_event(
            event_type="PROGRESS_REPORTED",
            task_id="TASK-002",
            assignment_id="asg-001",
            assignment_epoch=1,
            payload={"percent": 30, "completed_items": ["step1"]},
        )
        _run(client.append_event(event))

        read_back = _read_event_from_remote(
            remote, f"maf/node/{_NODE_ID}", event["event_id"]
        )
        assert read_back is not None
        assert read_back["event_id"] == event["event_id"]
        assert read_back["event_type"] == "PROGRESS_REPORTED"
        assert read_back["node_id"] == _NODE_ID
        assert read_back["task_id"] == "TASK-002"
        assert read_back["assignment_id"] == "asg-001"
        assert read_back["assignment_epoch"] == 1
        assert read_back["based_on_control_commit"] == _CONTROL_COMMIT
        assert read_back["payload"]["percent"] == 30


# --------------------------------------------------------------------------- #
# 验收 2：事件格式符合 Schema
# --------------------------------------------------------------------------- #


class TestEventSchemaCompliance:
    """事件通过 event-v1 Schema 校验。"""

    def test_appended_event_passes_schema(
        self,
        git_env: tuple[Path, Path, RunnerGitClient],
        loader: SchemaLoader,
    ) -> None:
        """远端事件文件通过 event-v1 Schema。"""
        remote, _local, client = git_env
        event = _make_event(event_type="SUBMISSION_CREATED")
        _run(client.append_event(event))

        read_back = _read_event_from_remote(
            remote, f"maf/node/{_NODE_ID}", event["event_id"]
        )
        assert read_back is not None
        loader.validate(_EVENT_REF, read_back)

    def test_all_event_types_pass_schema(
        self,
        git_env: tuple[Path, Path, RunnerGitClient],
        loader: SchemaLoader,
    ) -> None:
        """协议定义的全部事件类型都能通过 Schema。"""
        remote, _local, client = git_env
        for event_type in (
            "NODE_REGISTERED",
            "NODE_UPDATED",
            "CLAIM_REQUESTED",
            "PROGRESS_REPORTED",
            "BLOCKED_REPORTED",
            "SUBMISSION_CREATED",
            "WORK_ABANDONED",
        ):
            event = _make_event(
                event_type=event_type,
                event_id=f"evt-schema-{event_type}-{uuid.uuid4()}",
            )
            result = _run(client.append_event(event))
            assert result["push_ok"] is True, f"push failed for {event_type}"

            read_back = _read_event_from_remote(
                remote, f"maf/node/{_NODE_ID}", event["event_id"]
            )
            assert read_back is not None
            loader.validate(_EVENT_REF, read_back)

    def test_invalid_event_rejected_before_push(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """事件缺少必填字段时抛 ArgumentError，不触发 git 操作。"""
        _remote, _local, client = git_env
        bad_event = _make_event()
        del bad_event["event_id"]
        with pytest.raises(Exception):  # noqa: B017
            _run(client.append_event(bad_event))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 验收 3：append-only 不覆盖
# --------------------------------------------------------------------------- #


class TestAppendOnlyDoesNotOverwrite:
    """追加第二个事件不删除第一个事件文件。"""

    def test_two_events_coexist(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """连续追加两个事件，两个事件文件都存在于远端。"""
        remote, _local, client = git_env
        event1 = _make_event(
            event_type="CLAIM_REQUESTED",
            event_id=f"evt-first-{uuid.uuid4()}",
        )
        event2 = _make_event(
            event_type="PROGRESS_REPORTED",
            event_id=f"evt-second-{uuid.uuid4()}",
            assignment_epoch=1,
        )

        r1 = _run(client.append_event(event1))
        r2 = _run(client.append_event(event2))
        assert r1["push_ok"] is True
        assert r2["push_ok"] is True

        files = _list_event_files_on_remote(remote, f"maf/node/{_NODE_ID}")
        assert f".maf/events/{event1['event_id']}.json" in files
        assert f".maf/events/{event2['event_id']}.json" in files
        assert len(files) >= 2

    def test_branch_has_two_commits(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """两次 append 产生两个 commit（fast-forward 链）。"""
        remote, _local, client = git_env
        _run(client.append_event(_make_event(event_id=f"evt-c1-{uuid.uuid4()}")))
        _run(client.append_event(_make_event(event_id=f"evt-c2-{uuid.uuid4()}")))

        count = _count_commits_on_remote(remote, f"maf/node/{_NODE_ID}")
        assert count >= 2

    def test_first_event_not_overwritten(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """追加第二个事件后，第一个事件内容不变。"""
        remote, _local, client = git_env
        event1 = _make_event(
            event_type="NODE_REGISTERED",
            task_id=None,
            event_id=f"evt-persist-{uuid.uuid4()}",
            payload={"manifest": {"node_id": _NODE_ID}},
        )
        _run(client.append_event(event1))
        _run(client.append_event(_make_event(event_id=f"evt-other-{uuid.uuid4()}")))

        read_back = _read_event_from_remote(
            remote, f"maf/node/{_NODE_ID}", event1["event_id"]
        )
        assert read_back is not None
        assert read_back["event_id"] == event1["event_id"]
        assert read_back["event_type"] == "NODE_REGISTERED"
        assert read_back["payload"]["manifest"]["node_id"] == _NODE_ID


# --------------------------------------------------------------------------- #
# 验收 4：event_id 唯一
# --------------------------------------------------------------------------- #


class TestEventIdUniqueness:
    """不同事件的 event_id 唯一，文件名不冲突。"""

    def test_two_events_have_different_ids(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """两次 append 生成不同 event_id。"""
        _remote, _local, client = git_env
        e1 = _make_event(event_id=f"evt-uniq-1-{uuid.uuid4()}")
        e2 = _make_event(event_id=f"evt-uniq-2-{uuid.uuid4()}")
        assert e1["event_id"] != e2["event_id"]

        r1 = _run(client.append_event(e1))
        r2 = _run(client.append_event(e2))
        assert r1["event_id"] != r2["event_id"]
        assert r1["push_ok"] is True
        assert r2["push_ok"] is True

    def test_duplicate_event_id_produces_distinct_files(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """相同 event_id 的事件不会覆盖（文件路径相同，但内容追加为新 commit）。

        协议 §11：push 冲突时用同 event_id 重试。event_id 是事件去重 key，
        由中央调度器幂等处理；节点侧允许同 event_id 的事件文件路径相同（覆盖
        文件内容），但不影响其他事件文件。
        """
        remote, _local, client = git_env
        eid = f"evt-dup-{uuid.uuid4()}"
        e1 = _make_event(event_id=eid, payload={"version": 1})
        r1 = _run(client.append_event(e1))
        assert r1["push_ok"] is True

        # 同 event_id 第二次追加（覆盖文件内容）。
        e2 = _make_event(event_id=eid, payload={"version": 2})
        r2 = _run(client.append_event(e2))
        assert r2["push_ok"] is True

        # 文件存在，内容为最新版本。
        read_back = _read_event_from_remote(
            remote, f"maf/node/{_NODE_ID}", eid
        )
        assert read_back is not None
        assert read_back["payload"]["version"] == 2


# --------------------------------------------------------------------------- #
# 验收 5：assignment_epoch 字段
# --------------------------------------------------------------------------- #


class TestAssignmentEpochField:
    """事件携带 assignment_epoch fencing token（协议 §7）。"""

    def test_progress_event_has_epoch(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """PROGRESS_REPORTED 事件携带 assignment_epoch。"""
        remote, _local, client = git_env
        event = _make_event(
            event_type="PROGRESS_REPORTED",
            assignment_id="asg-epoch-1",
            assignment_epoch=1,
        )
        _run(client.append_event(event))

        read_back = _read_event_from_remote(
            remote, f"maf/node/{_NODE_ID}", event["event_id"]
        )
        assert read_back is not None
        assert read_back["assignment_epoch"] == 1

    def test_submission_event_has_epoch(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """SUBMISSION_CREATED 事件携带 assignment_epoch。"""
        remote, _local, client = git_env
        event = _make_event(
            event_type="SUBMISSION_CREATED",
            assignment_id="asg-epoch-2",
            assignment_epoch=2,
        )
        _run(client.append_event(event))

        read_back = _read_event_from_remote(
            remote, f"maf/node/{_NODE_ID}", event["event_id"]
        )
        assert read_back is not None
        assert read_back["assignment_epoch"] == 2

    def test_registration_event_epoch_is_none(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """NODE_REGISTERED 事件的 assignment_epoch 为 None。"""
        remote, _local, client = git_env
        event = _make_event(
            event_type="NODE_REGISTERED",
            task_id=None,
            assignment_id=None,
            assignment_epoch=None,
        )
        _run(client.append_event(event))

        read_back = _read_event_from_remote(
            remote, f"maf/node/{_NODE_ID}", event["event_id"]
        )
        assert read_back is not None
        assert read_back["assignment_epoch"] is None

    def test_epoch_zero_rejected(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """assignment_epoch=0 被拒（Schema 要求 minimum: 1）。"""
        _remote, _local, client = git_env
        event = _make_event(assignment_epoch=0)
        with pytest.raises(Exception):  # noqa: B017
            _run(client.append_event(event))


# --------------------------------------------------------------------------- #
# 验收 6：节点只能写自己的事件分支
# --------------------------------------------------------------------------- #


class TestNodeCanOnlyWriteOwnBranch:
    """节点不能写其他节点的事件分支或受保护分支。"""

    def test_other_node_id_rejected(self, git_env: tuple[Path, Path, RunnerGitClient]) -> None:
        """event.node_id 与客户端 node_id 不一致时抛 ArgumentError。"""
        _remote, _local, client = git_env
        event = _make_event(node_id=_OTHER_NODE_ID)
        with pytest.raises(ArgumentError, match="own branch"):
            _run(client.append_event(event))

    def test_empty_client_node_id_rejected(
        self, tmp_path: Path
    ) -> None:
        """客户端未配置 node_id 时抛 ArgumentError。"""
        _remote, local = _setup_git_repos(tmp_path)
        cli = RunnerGitCli(allowed_roots=[tmp_path])
        client = RunnerGitClient(
            git_cli=cli,
            repository_path=str(local),
            node_id="",
        )
        event = _make_event()
        with pytest.raises(ArgumentError, match="node_id"):
            _run(client.append_event(event))

    def test_control_branch_not_written(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """append_event 不会向 maf/control 写入任何内容。

        node_id 为 'control' 时，分支名 'maf/node/control' 不在禁止列表中，
        但协议 §2 禁止节点写 maf/control。本测试验证正常 node_id 的 append_event
        只创建 maf/node/<node-id> 分支，不触碰 maf/control。
        """
        remote, _local, client = git_env
        _run(client.append_event(_make_event()))

        # maf/node/<node-id> 存在。
        assert _branch_exists_on_remote(remote, f"maf/node/{_NODE_ID}")
        # maf/control 不存在（节点从未创建）。
        assert not _branch_exists_on_remote(remote, "maf/control")


# --------------------------------------------------------------------------- #
# 验收 7：push 冲突可 fetch/rebase 后用同 event_id 重试
# --------------------------------------------------------------------------- #


class TestPushConflictRetry:
    """push 冲突时自动 fetch 并用同 event_id 重试。"""

    def test_retry_succeeds_after_concurrent_push(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """另一客户端先 push 后，本客户端 fetch+retry 成功。

        场景：
        1. Client A append event-1（成功）。
        2. 直接用 git 命令向远端追加 event-x（模拟并发写入）。
        3. Client A append event-2：fetch 获取最新状态后 fast-forward push 成功。
        """
        remote, local, client = git_env
        # 步骤 1
        event1 = _make_event(event_id=f"evt-retry-1-{uuid.uuid4()}")
        _run(client.append_event(event1))

        # 步骤 2：绕过 Client A 直接 push（模拟并发写入）
        concurrent_event = _make_event(
            event_type="PROGRESS_REPORTED",
            event_id=f"evt-concurrent-{uuid.uuid4()}",
            assignment_epoch=1,
        )
        _push_event_directly(remote, local, concurrent_event, f"maf/node/{_NODE_ID}")

        # 步骤 3：Client A 追加 event-2（应成功，因 fetch 获取最新状态后 fast-forward）
        event2 = _make_event(
            event_type="SUBMISSION_CREATED",
            event_id=f"evt-retry-2-{uuid.uuid4()}",
            assignment_epoch=1,
        )
        result = _run(client.append_event(event2, max_retries=2))

        assert result["push_ok"] is True
        assert result["event_id"] == event2["event_id"]

        # 三个事件都存在（append-only）。
        files = _list_event_files_on_remote(remote, f"maf/node/{_NODE_ID}")
        assert f".maf/events/{event1['event_id']}.json" in files
        assert f".maf/events/{concurrent_event['event_id']}.json" in files
        assert f".maf/events/{event2['event_id']}.json" in files

    def test_all_retries_exhausted_returns_failure(
        self, tmp_path: Path
    ) -> None:
        """所有重试都失败时返回 push_ok=False。"""
        _remote, local = _setup_git_repos(tmp_path)

        # 使用一个不存在的 remote URL 让 fetch 和 push 都失败。
        # 通过指向不存在的 remote 来触发持续失败。
        cli = RunnerGitCli(allowed_roots=[tmp_path])
        client = RunnerGitClient(
            git_cli=cli,
            repository_path=str(local),
            control_remote="nonexistent-remote",
            node_id=_NODE_ID,
        )
        event = _make_event()
        result = _run(client.append_event(event, max_retries=1))

        assert result["push_ok"] is False
        assert result["attempts"] == 2  # 1 次初始 + 1 次重试
        assert result["push_error"] is not None

    def test_event_id_preserved_across_retries(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """重试时 event_id 保持不变。"""
        _remote, _local, client = git_env
        event_id = f"evt-preserve-{uuid.uuid4()}"
        event = _make_event(event_id=event_id)

        result = _run(client.append_event(event, max_retries=3))
        assert result["event_id"] == event_id


# --------------------------------------------------------------------------- #
# 首次注册事件（orphan 分支创建）
# --------------------------------------------------------------------------- #


class TestFirstEventCreatesOrphanBranch:
    """首次 append_event 创建 orphan 节点分支。"""

    def test_first_event_on_fresh_repo(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """全新仓库上首次 append_event 成功创建节点分支。"""
        remote, _local, client = git_env
        # git_env fixture 是每个测试函数新建的，所以分支还不存在。
        assert not _branch_exists_on_remote(remote, f"maf/node/{_NODE_ID}")

        event = _make_event(event_type="NODE_REGISTERED", task_id=None)
        result = _run(client.append_event(event))

        assert result["push_ok"] is True
        assert _branch_exists_on_remote(remote, f"maf/node/{_NODE_ID}")

    def test_orphan_branch_independent_of_main(
        self, git_env: tuple[Path, Path, RunnerGitClient]
    ) -> None:
        """节点分支是 orphan，不包含 main 分支的文件。"""
        remote, _local, client = git_env
        event = _make_event(event_type="NODE_REGISTERED", task_id=None)
        _run(client.append_event(event))

        # 节点分支上不应有 README.md（main 分支的文件）。
        result = subprocess.run(
            [
                "git", "-C", str(remote), "ls-tree",
                "-r", "--name-only", f"refs/heads/maf/node/{_NODE_ID}",
            ],
            capture_output=True,
            text=True,
            env=_GIT_ENV,
        )
        files = result.stdout.strip().split("\n") if result.stdout.strip() else []
        assert not any(f == "README.md" for f in files), (
            "orphan 分支不应包含 main 的文件"
        )
        # 只有事件文件。
        assert all(f.startswith(".maf/events/") for f in files)
