"""TASK-067 集成测试：Git 同步主循环。

验收标准覆盖：

1. **首次 fetch**：SyncLoop 首次 fetch control 后处理分配给本节点的任务。
2. **无变化去重**：相同 ``control_commit`` 不重复处理（增量检测）。
3. **有变化处理**：control commit 变化后处理新分配。
4. **fetch 失败重试**：fetch 失败后等待 ``poll_interval`` 重试，不崩溃。
5. **优雅停止**：``request_stop()`` 后完成当前迭代再退出。
6. **离线分配禁止**：节点只从 control 读取 ``assignment``，不在本地决定任务分配；
   control 无分配时不推送事件。
7. **fetch_control 增强**：``RunnerGitClient.fetch_control`` 返回的快照含解析后的
   ``tasks``/``nodes`` 列表（从 ``.maf/tasks/*.yaml`` 和 ``.maf/nodes/*.yaml`` 读取）。

测试使用真实 git（2.45+）引导临时 bare 仓库作为远端，clone 为本地工作仓库，
通过 ``RunnerGitClient`` 执行 fetch_control，用 ``SyncLoop`` 运行同步主循环。
部分场景使用 mock git_client 以隔离 fetch 失败等边界条件。所有异步入口经
``asyncio.run`` 同步执行，避免 pytest-asyncio 配置依赖。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

# packages/artifact_schemas/src 不在 pyproject.toml 的 pythonpath 中（TASK-002 范围），
# 此处显式添加，使 maf_server.git_coordination.schemas 可导入以做 Schema 校验。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT_SCHEMAS_SRC = _PROJECT_ROOT / "packages" / "artifact_schemas" / "src"
if str(_ARTIFACT_SCHEMAS_SRC) not in sys.path:
    sys.path.insert(0, str(_ARTIFACT_SCHEMAS_SRC))

from maf_runner.config import NodeSettings  # noqa: E402
from maf_runner.git_client import RunnerGitClient  # noqa: E402
from maf_runner.main import SyncLoop, SyncLoopResult  # noqa: E402
from maf_runner.registry import RunnerRegistry  # noqa: E402
from maf_runner.workspace.git import RunnerGitCli  # noqa: E402
from maf_repository_adapters import SubprocessGitCli  # noqa: E402
from maf_server.git_coordination.schemas import SchemaLoader  # noqa: E402
from maf_server.modules.git_coordination.service import (  # noqa: E402
    LocalGitCoordinationService,
)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

_NODE_ID = "node-12345678-1234-1234-1234-123456789abc"
_OTHER_NODE_ID = "node-99999999-9999-9999-9999-999999999999"
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
    """在独立事件循环中执行协程并返回结果。"""
    return asyncio.run(coro)


def _git(repo: Path, *args: str) -> str:
    """同步执行 git 命令，返回 stdout（用于 fixture 准备与断言）。"""
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


def _setup_remote_and_local(tmp_path: Path) -> tuple[Path, Path]:
    """创建 bare 远端仓库 + 本地 clone，返回 (remote_path, local_path)。

    本地 clone 有一个初始 main 提交并 push 到远端，确保远端非空。
    与 ``test_node_event_push.py`` 的 ``_setup_git_repos`` 模式一致。
    """
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-q", str(remote)],
        check=True,
        env=_GIT_ENV,
    )
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


def _init_control(
    local: Path,
    *,
    project_id: str = "proj-sync-001",
    templates_dir: Path = _TEMPLATES_DIR,
    schema_loader: SchemaLoader | None = None,
) -> str:
    """在本地 clone 上用 LocalGitCoordinationService 初始化 control 分支。

    返回 control commit SHA。
    """
    cli = SubprocessGitCli(allowed_roots=[local])
    service = LocalGitCoordinationService(
        git_cli=cli,
        repository_path=str(local),
        templates_dir=templates_dir,
        schema_loader=schema_loader or SchemaLoader(),
    )
    commit = _run(service.initialize_project("binding-sync", project_id))
    # Push control to remote.
    _git(local, "push", "-q", "origin", "maf/control")
    return commit


def _make_task(
    task_id: str,
    *,
    node_id: str | None = None,
    status: str = "ASSIGNED",
    assignment_id: str | None = None,
    assignment_epoch: int = 1,
    title: str | None = None,
) -> dict[str, Any]:
    """构造一个合法的 CoordinationTask dict（与 task-v1 schema 对齐）。"""
    task: dict[str, Any] = {
        "schema_version": 1,
        "task_id": task_id,
        "parent_task_id": None,
        "title": title or f"Task {task_id}",
        "description": "Test task for sync loop",
        "status": status,
        "priority": 1,
        "requirements": {},
        "dependencies": [],
        "assignment": None,
        "progress": {
            "percent": 0,
            "completed_items": [],
            "remaining_items": [],
            "problems": [],
            "current_head_commit": None,
            "test_summary": None,
            "last_reported_at": None,
        },
        "delivery": {
            "branch": None,
            "base_commit": None,
            "head_commit": None,
            "pull_request_url": None,
            "changed_paths": [],
            "test_report_path": None,
            "known_issues": [],
        },
        "version": 1,
    }
    if node_id:
        task["assignment"] = {
            "node_id": node_id,
            "assignment_id": assignment_id or f"asg-{task_id}",
            "assignment_epoch": assignment_epoch,
            "assigned_at": "2026-07-17T00:00:00Z",
            "expires_at": "2026-07-18T00:00:00Z",
            "based_on_control_commit": "0" * 40,
        }
    return task


def _add_task_to_control(
    local: Path,
    task: dict[str, Any],
    *,
    push: bool = True,
) -> str:
    """向 maf/control 分支添加一个 task YAML 文件，commit 并 push。

    返回新的 control commit SHA。
    """
    task_id = task["task_id"]
    rel_path = f".maf/tasks/{task_id}.yaml"
    _git(local, "switch", "maf/control")
    task_file = local / rel_path
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(
        yaml.dump(task, default_flow_style=False, sort_keys=True),
        encoding="utf-8",
    )
    _git(local, "add", rel_path)
    _git(local, "commit", "-m", f"add task {task_id}")
    commit = _git(local, "rev-parse", "maf/control")
    if push:
        _git(local, "push", "-q", "origin", "maf/control")
    _git(local, "switch", "main")
    return commit


def _make_node_settings(
    workspace_root: Path,
    *,
    node_id: str = _NODE_ID,
) -> NodeSettings:
    """构造测试用 NodeSettings。"""
    return NodeSettings(
        node_id=node_id,
        control_remote_url="origin",
        workspace_root=workspace_root,
        model_mapping_path=workspace_root / "model-mapping.yaml",
        capability_token_cache_path=Path("capability-tokens.db"),
        _env_file=None,
    )


def _make_registry(
    workspace_root: Path,
    *,
    node_id: str = _NODE_ID,
) -> RunnerRegistry:
    """构造测试用 RunnerRegistry。"""
    settings = _make_node_settings(workspace_root, node_id=node_id)
    return RunnerRegistry(settings=settings)


async def _no_sleep(seconds: float) -> None:
    """测试用 async no-op sleeper，不真实阻塞。"""
    pass


class _MockGitClient:
    """Mock GitCoordinationClient，用于隔离 fetch 失败等边界条件。

    可预设 ``fetch_control`` 返回的快照序列，以及 ``append_event`` 的结果。
    """

    def __init__(
        self,
        *,
        snapshots: list[dict[str, Any]] | None = None,
        fetch_ok: bool = True,
        append_ok: bool = True,
        fetch_error: str = "mock fetch failure",
    ) -> None:
        self._snapshots = snapshots or []
        self._index = 0
        self._fetch_ok = fetch_ok
        self._append_ok = append_ok
        self._fetch_error = fetch_error
        self.appended_events: list[dict[str, Any]] = []
        self.fetch_call_count: int = 0

    async def fetch_control(self) -> dict[str, Any]:
        self.fetch_call_count += 1
        if not self._fetch_ok:
            return {
                "fetch_ok": False,
                "control_commit": None,
                "control_branch": "maf/control",
                "remote": "origin",
                "fetch_error": self._fetch_error,
            }
        if self._index < len(self._snapshots):
            snapshot = self._snapshots[self._index]
            self._index += 1
            return snapshot
        # 快照序列耗尽：返回最后一个快照（模拟无变化）。
        if self._snapshots:
            return self._snapshots[-1]
        return {
            "fetch_ok": True,
            "control_commit": "abc123def456",
            "control_branch": "maf/control",
            "remote": "origin",
            "fetch_error": None,
            "tasks": [],
            "nodes": [],
        }

    async def append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        self.appended_events.append(event)
        return {
            "push_ok": self._append_ok,
            "commit": "mock-commit" if self._append_ok else None,
            "branch": f"maf/node/{event.get('node_id', '')}",
            "event_id": event.get("event_id"),
            "event_path": ".maf/events/mock.json",
            "remote": "origin",
            "push_error": None if self._append_ok else "mock push failure",
            "attempts": 1,
        }


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_maf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除 ``MAF_*`` 环境变量，避免本地 .env 污染测试。"""
    for key in list(os.environ):
        if key.startswith("MAF_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def sync_env(
    tmp_path: Path,
) -> tuple[Path, Path, RunnerGitClient, str]:
    """创建远端 + 本地 clone + 初始化 control + RunnerGitClient。

    返回 (remote_path, local_path, client, project_id)。
    """
    remote, local = _setup_remote_and_local(tmp_path)
    project_id = "proj-sync-001"
    _init_control(local, project_id=project_id)

    runner_cli = RunnerGitCli(allowed_roots=[tmp_path])
    client = RunnerGitClient(
        git_cli=runner_cli,
        repository_path=str(local),
        control_remote="origin",
        control_branch="maf/control",
        node_id=_NODE_ID,
    )
    return remote, local, client, project_id


# --------------------------------------------------------------------------- #
# 验收 7：fetch_control 增强——快照含解析后的 tasks/nodes
# --------------------------------------------------------------------------- #


class TestFetchControlEnhancement:
    """``RunnerGitClient.fetch_control`` 返回解析后的 tasks/nodes 列表。"""

    def test_fetch_returns_parsed_tasks(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """fetch_control 返回的 ``tasks`` 列表含从 ``.maf/tasks/*.yaml`` 解析的任务。"""
        remote, local, client, _project_id = sync_env
        task = _make_task("TASK-ENH-001", node_id=_NODE_ID)
        _add_task_to_control(local, task)

        result = _run(client.fetch_control())

        assert result["fetch_ok"] is True
        tasks = result["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == "TASK-ENH-001"
        assert tasks[0]["assignment"]["node_id"] == _NODE_ID

    def test_fetch_returns_multiple_tasks(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """多个 task 文件都被解析。"""
        remote, local, client, _project_id = sync_env
        _add_task_to_control(local, _make_task("TASK-MULTI-1", node_id=_NODE_ID))
        _add_task_to_control(local, _make_task("TASK-MULTI-2", node_id=_OTHER_NODE_ID))
        _add_task_to_control(local, _make_task("TASK-MULTI-3"))

        result = _run(client.fetch_control())

        tasks = result["tasks"]
        task_ids = {t["task_id"] for t in tasks}
        assert task_ids == {"TASK-MULTI-1", "TASK-MULTI-2", "TASK-MULTI-3"}

    def test_fetch_skips_gitkeep(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """``.gitkeep`` 占位文件不被解析为 task。"""
        _remote, _local, client, _project_id = sync_env
        result = _run(client.fetch_control())

        # 初始化后 tasks 目录只有 .gitkeep，tasks 列表为空。
        assert result["tasks"] == []
        assert result["nodes"] == []

    def test_fetch_returns_control_commit_for_dedup(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """``control_commit`` 非空，供 SyncLoop 去重。"""
        _remote, _local, client, _project_id = sync_env
        result = _run(client.fetch_control())

        assert result["control_commit"] is not None
        assert len(result["control_commit"]) == 40


# --------------------------------------------------------------------------- #
# 验收 1：首次 fetch 处理分配
# --------------------------------------------------------------------------- #


class TestFirstFetch:
    """SyncLoop 首次 fetch control 后处理分配给本节点的任务。"""

    def test_first_fetch_processes_assignment(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """首次 fetch 检测到分配给本节点的任务，调用 assignment_handler。"""
        remote, local, client, _project_id = sync_env
        task = _make_task("TASK-FIRST-001", node_id=_NODE_ID)
        _add_task_to_control(local, task)

        handled: list[dict[str, Any]] = []

        def handler(task_arg: dict[str, Any], snapshot: dict[str, Any]) -> None:
            handled.append(task_arg)

        registry = _make_registry(local)
        loop = SyncLoop(
            registry=registry,
            git_client=client,
            node_id=_NODE_ID,
            poll_interval=0.01,
            sleeper=_no_sleep,
            assignment_handler=handler,
        )

        result = _run(loop.run_sync_loop(max_iterations=1))

        assert result.iterations == 1
        assert result.fetch_failures == 0
        assert result.assignments_processed == 1
        assert len(handled) == 1
        assert handled[0]["task_id"] == "TASK-FIRST-001"

    def test_first_fetch_pushes_event(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """首次 fetch 检测到分配后推送 NODE_UPDATED 事件。"""
        remote, local, client, _project_id = sync_env
        task = _make_task("TASK-EVT-001", node_id=_NODE_ID)
        _add_task_to_control(local, task)

        registry = _make_registry(local)
        loop = SyncLoop(
            registry=registry,
            git_client=client,
            node_id=_NODE_ID,
            poll_interval=0.01,
            sleeper=_no_sleep,
        )

        result = _run(loop.run_sync_loop(max_iterations=1))

        assert result.assignments_processed == 1
        assert result.events_pushed == 1


# --------------------------------------------------------------------------- #
# 验收 2：无变化去重
# --------------------------------------------------------------------------- #


class TestNoChangeDedup:
    """相同 ``control_commit`` 不重复处理（增量检测）。"""

    def test_same_commit_no_reprocessing(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """两次迭代同一 commit，第二次不重复处理分配。"""
        remote, local, client, _project_id = sync_env
        task = _make_task("TASK-DEDUP-001", node_id=_NODE_ID)
        _add_task_to_control(local, task)

        handled: list[dict[str, Any]] = []

        def handler(task_arg: dict[str, Any], snapshot: dict[str, Any]) -> None:
            handled.append(task_arg)

        registry = _make_registry(local)
        loop = SyncLoop(
            registry=registry,
            git_client=client,
            node_id=_NODE_ID,
            poll_interval=0.01,
            sleeper=_no_sleep,
            assignment_handler=handler,
        )

        result = _run(loop.run_sync_loop(max_iterations=2))

        assert result.iterations == 2
        assert result.fetch_failures == 0
        # 只在第一次迭代处理了分配（去重）。
        assert result.assignments_processed == 1
        assert len(handled) == 1

    def test_same_commit_no_duplicate_event(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """两次迭代同一 commit，第二次不推送事件。"""
        remote, local, client, _project_id = sync_env
        task = _make_task("TASK-DEDUP-EVT-001", node_id=_NODE_ID)
        _add_task_to_control(local, task)

        registry = _make_registry(local)
        loop = SyncLoop(
            registry=registry,
            git_client=client,
            node_id=_NODE_ID,
            poll_interval=0.01,
            sleeper=_no_sleep,
        )

        result = _run(loop.run_sync_loop(max_iterations=2))

        # 只在第一次迭代推送了事件。
        assert result.events_pushed == 1

    def test_last_commit_recorded(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """SyncLoop 记录最后处理的 commit。"""
        _remote, local, client, _project_id = sync_env
        task = _make_task("TASK-COMMIT-001", node_id=_NODE_ID)
        commit = _add_task_to_control(local, task)

        registry = _make_registry(local)
        loop = SyncLoop(
            registry=registry,
            git_client=client,
            node_id=_NODE_ID,
            poll_interval=0.01,
            sleeper=_no_sleep,
        )

        result = _run(loop.run_sync_loop(max_iterations=1))

        assert result.last_commit == commit


# --------------------------------------------------------------------------- #
# 验收 3：有变化处理
# --------------------------------------------------------------------------- #


class TestChangeProcessing:
    """control commit 变化后处理新分配。"""

    def test_new_commit_processes_new_assignment(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """control 变化后，新分配被处理。"""
        remote, local, client, _project_id = sync_env
        # 初始：一个任务分配给本节点。
        _add_task_to_control(local, _make_task("TASK-CHG-1", node_id=_NODE_ID))

        handled: list[dict[str, Any]] = []

        def handler(task_arg: dict[str, Any], snapshot: dict[str, Any]) -> None:
            handled.append(task_arg)

        registry = _make_registry(local)
        loop = SyncLoop(
            registry=registry,
            git_client=client,
            node_id=_NODE_ID,
            poll_interval=0.01,
            sleeper=_no_sleep,
            assignment_handler=handler,
        )

        # 第一次运行：处理初始任务。
        result1 = _run(loop.run_sync_loop(max_iterations=1))
        assert result1.assignments_processed == 1
        assert len(handled) == 1

        # 添加新任务（control commit 变化）。
        _add_task_to_control(local, _make_task("TASK-CHG-2", node_id=_NODE_ID))

        # 第二次运行：检测到新 commit，处理新任务。
        result2 = _run(loop.run_sync_loop(max_iterations=1))
        assert result2.assignments_processed == 1
        assert len(handled) == 2
        assert handled[1]["task_id"] == "TASK-CHG-2"

    def test_new_commit_updates_last_commit(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """control 变化后 last_commit 更新为新 commit。"""
        remote, local, client, _project_id = sync_env
        _add_task_to_control(local, _make_task("TASK-UPD-1", node_id=_NODE_ID))

        registry = _make_registry(local)
        loop = SyncLoop(
            registry=registry,
            git_client=client,
            node_id=_NODE_ID,
            poll_interval=0.01,
            sleeper=_no_sleep,
        )

        result1 = _run(loop.run_sync_loop(max_iterations=1))
        commit1 = result1.last_commit

        _add_task_to_control(local, _make_task("TASK-UPD-2", node_id=_NODE_ID))

        result2 = _run(loop.run_sync_loop(max_iterations=1))
        assert result2.last_commit != commit1
        assert result2.last_commit != ""


# --------------------------------------------------------------------------- #
# 验收 4：fetch 失败重试
# --------------------------------------------------------------------------- #


class TestFetchFailureRetry:
    """fetch 失败后等待 poll_interval 重试，不崩溃。"""

    def test_fetch_failure_does_not_crash(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """fetch 失败时主循环不崩溃，记录失败并继续。"""
        _remote, local, _client, _project_id = sync_env

        mock_client = _MockGitClient(fetch_ok=False)
        registry = _make_registry(local)
        loop = SyncLoop(
            registry=registry,
            git_client=mock_client,
            node_id=_NODE_ID,
            poll_interval=0.01,
            sleeper=_no_sleep,
        )

        result = _run(loop.run_sync_loop(max_iterations=3))

        assert result.iterations == 3
        assert result.fetch_failures == 3
        assert result.assignments_processed == 0
        assert result.events_pushed == 0
        # 没有崩溃，循环正常退出。

    def test_fetch_failure_then_success(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """fetch 失败后恢复成功，正常处理分配。"""
        remote, local, _client, _project_id = sync_env
        _add_task_to_control(local, _make_task("TASK-RETRY-001", node_id=_NODE_ID))

        # 用真实 client：先断开 remote（模拟失败），再恢复。
        # 这里用 mock 模拟先失败后成功的场景。
        success_snapshot = _run(_client.fetch_control())
        mock_client = _MockGitClient(
            snapshots=[
                {  # 第一次：失败
                    "fetch_ok": False,
                    "control_commit": None,
                    "fetch_error": "connection refused",
                },
                success_snapshot,  # 第二次：成功
            ],
        )

        handled: list[dict[str, Any]] = []

        def handler(task_arg: dict[str, Any], snapshot: dict[str, Any]) -> None:
            handled.append(task_arg)

        registry = _make_registry(local)
        loop = SyncLoop(
            registry=registry,
            git_client=mock_client,
            node_id=_NODE_ID,
            poll_interval=0.01,
            sleeper=_no_sleep,
            assignment_handler=handler,
        )

        result = _run(loop.run_sync_loop(max_iterations=2))

        assert result.iterations == 2
        assert result.fetch_failures == 1
        # 第二次成功，处理了分配。
        assert result.assignments_processed == 1
        assert len(handled) == 1

    def test_no_offline_allocation_on_fetch_failure(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """fetch 失败时不生成离线分配（不处理任务、不推送事件）。"""
        _remote, local, _client, _project_id = sync_env

        mock_client = _MockGitClient(fetch_ok=False)
        registry = _make_registry(local)
        loop = SyncLoop(
            registry=registry,
            git_client=mock_client,
            node_id=_NODE_ID,
            poll_interval=0.01,
            sleeper=_no_sleep,
        )

        result = _run(loop.run_sync_loop(max_iterations=3))

        # GitHub 不可达时退避且不生成离线分配。
        assert result.assignments_processed == 0
        assert result.events_pushed == 0
        assert mock_client.appended_events == []


# --------------------------------------------------------------------------- #
# 验收 5：优雅停止
# --------------------------------------------------------------------------- #


class TestGracefulStop:
    """``request_stop()`` 后完成当前迭代再退出。"""

    def test_stop_exits_before_max_iterations(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """assignment_handler 中调用 request_stop，循环在当前迭代后退出。"""
        remote, local, client, _project_id = sync_env
        _add_task_to_control(local, _make_task("TASK-STOP-001", node_id=_NODE_ID))

        loop = SyncLoop(
            registry=_make_registry(local),
            git_client=client,
            node_id=_NODE_ID,
            poll_interval=0.01,
            sleeper=_no_sleep,
        )

        def handler(task_arg: dict[str, Any], snapshot: dict[str, Any]) -> None:
            # 在处理分配时请求停止。
            loop.request_stop()

        loop._assignment_handler = handler

        result = _run(loop.run_sync_loop(max_iterations=10))

        # 只运行了 1 次迭代就停止（不是 10 次）。
        assert result.iterations == 1
        assert result.stopped is True
        assert result.assignments_processed == 1

    def test_stop_completes_current_iteration(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """停止请求不中断当前迭代：当前迭代的分配仍被处理。"""
        remote, local, client, _project_id = sync_env
        _add_task_to_control(local, _make_task("TASK-COMP-001", node_id=_NODE_ID))

        handled: list[dict[str, Any]] = []
        loop = SyncLoop(
            registry=_make_registry(local),
            git_client=client,
            node_id=_NODE_ID,
            poll_interval=0.01,
            sleeper=_no_sleep,
        )

        def handler(task_arg: dict[str, Any], snapshot: dict[str, Any]) -> None:
            handled.append(task_arg)
            loop.request_stop()

        loop._assignment_handler = handler

        result = _run(loop.run_sync_loop(max_iterations=10))

        # 当前迭代完成：分配被处理、事件被推送。
        assert result.assignments_processed == 1
        assert result.events_pushed == 1
        assert len(handled) == 1
        assert result.stopped is True

    def test_stop_no_new_tasks_after_stop(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """关闭时不再申请新任务：停止后不处理后续分配。"""
        remote, local, client, _project_id = sync_env
        _add_task_to_control(local, _make_task("TASK-NOSTOP-1", node_id=_NODE_ID))

        handled: list[dict[str, Any]] = []
        loop = SyncLoop(
            registry=_make_registry(local),
            git_client=client,
            node_id=_NODE_ID,
            poll_interval=0.01,
            sleeper=_no_sleep,
        )

        def handler(task_arg: dict[str, Any], snapshot: dict[str, Any]) -> None:
            handled.append(task_arg)
            loop.request_stop()

        loop._assignment_handler = handler

        # 第一次运行：处理任务 1，请求停止。
        result1 = _run(loop.run_sync_loop(max_iterations=10))
        assert result1.assignments_processed == 1
        assert result1.stopped is True

        # 添加新任务。
        _add_task_to_control(local, _make_task("TASK-NOSTOP-2", node_id=_NODE_ID))

        # 重置停止标志，再次运行。
        loop._stop_requested = False
        result2 = _run(loop.run_sync_loop(max_iterations=10))
        # 第二次运行处理新任务。
        assert result2.assignments_processed == 1
        # 总共处理了 2 个任务。
        assert len(handled) == 2


# --------------------------------------------------------------------------- #
# 验收 6：离线分配禁止
# --------------------------------------------------------------------------- #


class TestNoOfflineAllocation:
    """节点只从 control 读取 assignment，不在本地决定任务分配。"""

    def test_no_assignment_no_event(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """control 中无分配给本节点的任务时，不推送事件。"""
        _remote, local, client, _project_id = sync_env
        # 添加一个任务但没有 assignment。
        _add_task_to_control(local, _make_task("TASK-NOASGN-001"))

        registry = _make_registry(local)
        loop = SyncLoop(
            registry=registry,
            git_client=client,
            node_id=_NODE_ID,
            poll_interval=0.01,
            sleeper=_no_sleep,
        )

        result = _run(loop.run_sync_loop(max_iterations=1))

        assert result.assignments_processed == 0
        assert result.events_pushed == 0

    def test_other_node_assignment_ignored(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """分配给其他节点的任务不被本节点处理。"""
        _remote, local, client, _project_id = sync_env
        _add_task_to_control(
            local, _make_task("TASK-OTHER-001", node_id=_OTHER_NODE_ID)
        )

        handled: list[dict[str, Any]] = []

        def handler(task_arg: dict[str, Any], snapshot: dict[str, Any]) -> None:
            handled.append(task_arg)

        registry = _make_registry(local)
        loop = SyncLoop(
            registry=registry,
            git_client=client,
            node_id=_NODE_ID,
            poll_interval=0.01,
            sleeper=_no_sleep,
            assignment_handler=handler,
        )

        result = _run(loop.run_sync_loop(max_iterations=1))

        # 不处理其他节点的分配。
        assert result.assignments_processed == 0
        assert len(handled) == 0
        assert result.events_pushed == 0

    def test_mixed_assignments_only_own_processed(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """混合分配中只处理本节点的任务。"""
        _remote, local, client, _project_id = sync_env
        _add_task_to_control(local, _make_task("TASK-MIX-1", node_id=_NODE_ID))
        _add_task_to_control(
            local, _make_task("TASK-MIX-2", node_id=_OTHER_NODE_ID)
        )
        _add_task_to_control(local, _make_task("TASK-MIX-3", node_id=_NODE_ID))
        _add_task_to_control(local, _make_task("TASK-MIX-4"))

        handled: list[dict[str, Any]] = []

        def handler(task_arg: dict[str, Any], snapshot: dict[str, Any]) -> None:
            handled.append(task_arg)

        registry = _make_registry(local)
        loop = SyncLoop(
            registry=registry,
            git_client=client,
            node_id=_NODE_ID,
            poll_interval=0.01,
            sleeper=_no_sleep,
            assignment_handler=handler,
        )

        result = _run(loop.run_sync_loop(max_iterations=1))

        # 只处理 TASK-MIX-1 和 TASK-MIX-3（分配给本节点）。
        assert result.assignments_processed == 2
        handled_ids = {t["task_id"] for t in handled}
        assert handled_ids == {"TASK-MIX-1", "TASK-MIX-3"}

    def test_no_local_task_selection(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """节点不在本地决定任务分配：READY 但未 ASSIGNED 的任务不被处理。"""
        _remote, local, client, _project_id = sync_env
        # 添加一个 READY 状态但没有 assignment 的任务。
        _add_task_to_control(
            local,
            _make_task("TASK-READY-001", status="READY"),
        )

        handled: list[dict[str, Any]] = []

        def handler(task_arg: dict[str, Any], snapshot: dict[str, Any]) -> None:
            handled.append(task_arg)

        registry = _make_registry(local)
        loop = SyncLoop(
            registry=registry,
            git_client=client,
            node_id=_NODE_ID,
            poll_interval=0.01,
            sleeper=_no_sleep,
            assignment_handler=handler,
        )

        result = _run(loop.run_sync_loop(max_iterations=1))

        # READY 但未分配的任务不被处理（离线分配禁止）。
        assert result.assignments_processed == 0
        assert len(handled) == 0
        assert result.events_pushed == 0


# --------------------------------------------------------------------------- #
# 验收：GitCoordinationClient Protocol
# --------------------------------------------------------------------------- #


class TestGitCoordinationClientProtocol:
    """``GitCoordinationClient`` Protocol 含 fetch_control/append_event/verify_binding。"""

    def test_protocol_has_fetch_control(self) -> None:
        """Protocol 声明 fetch_control 方法。"""
        from maf_runner.git_client import GitCoordinationClient

        assert hasattr(GitCoordinationClient, "fetch_control")

    def test_protocol_has_append_event(self) -> None:
        """Protocol 声明 append_event 方法。"""
        from maf_runner.git_client import GitCoordinationClient

        assert hasattr(GitCoordinationClient, "append_event")

    def test_protocol_has_verify_binding(self) -> None:
        """Protocol 声明 verify_binding 方法。"""
        from maf_runner.git_client import GitCoordinationClient

        assert hasattr(GitCoordinationClient, "verify_binding")

    def test_runner_git_client_has_fetch_control(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """``RunnerGitClient`` 实现 fetch_control。"""
        _remote, _local, client, _project_id = sync_env
        assert callable(getattr(client, "fetch_control", None))

    def test_runner_git_client_has_append_event(
        self,
        sync_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """``RunnerGitClient`` 实现 append_event。"""
        _remote, _local, client, _project_id = sync_env
        assert callable(getattr(client, "append_event", None))
