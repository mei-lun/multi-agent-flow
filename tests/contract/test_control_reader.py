"""TASK-016 契约测试：读取 Control 快照。

验收标准覆盖（对应 TASK-016 文档）：

1. **快照包含 control commit、任务和节点**：``fetch_control`` 返回的
   :class:`CoordinationSnapshot` 含 ``control_commit``、``tasks_paths``、
   ``nodes_paths``、``events_paths``、``project_yaml``、``status_md``、
   ``commit_timestamp``、``generated_at`` 等字段。
2. **非 fast-forward 或 Schema 错误时不返回部分快照**：``fetch_control``
   在 ``project_id`` 不一致、``.maf/project.yaml`` Schema 错误、control 分支
   不存在等情况下抛异常，绝不返回部分快照。
3. **读取不改变工作区文件**：``fetch_control`` 调用前后 main 工作树文件
   不变、control 分支 commit 不变（只读语义）。
4. **commit 去重**：相同 commit 的重复调用返回相同 ``control_commit``，
   调用方可据此跳过重复处理。

测试使用 ``tests/fixtures/git_repo.py`` 的 ``init_local_git_repo`` 创建真实
git 仓库，用 TASK-015 的 ``initialize_project`` 初始化 control 分支，再调用
``fetch_control`` 验证快照内容。Runner 端使用 bare 远端 + clone 模式
（与 ``test_node_event_push.py`` 一致）。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

# packages/artifact_schemas/src 尚未加入 pyproject.toml pythonpath（TASK-002 范围），
# 此处显式添加，使 maf_artifact_schemas 可被 maf_server.git_coordination.schemas
# 与 maf_server.modules.git_coordination.service 导入。与现有
# tests/contract/test_coordination_schema.py 一致。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT_SCHEMAS_SRC = _PROJECT_ROOT / "packages" / "artifact_schemas" / "src"
if str(_ARTIFACT_SCHEMAS_SRC) not in sys.path:
    sys.path.insert(0, str(_ARTIFACT_SCHEMAS_SRC))

from maf_artifact_schemas.protocol import ProtocolVersion, SchemaRef  # noqa: E402
from maf_domain.errors import (  # noqa: E402
    ArgumentError,
    UnsupportedOperationError,
    ValidationError,
)
from maf_repository_adapters import SubprocessGitCli  # noqa: E402
from maf_runner.git_client import RunnerGitClient  # noqa: E402
from maf_runner.workspace.git import RunnerGitCli  # noqa: E402
from maf_server.git_coordination.schemas import SchemaLoader  # noqa: E402
from maf_server.modules.git_coordination.service import (  # noqa: E402
    CoordinationSnapshot,
    LocalGitCoordinationService,
)

# 导入 tests/fixtures/git_repo.py 的 init_local_git_repo 工厂。
_FIXTURES_DIR = _PROJECT_ROOT / "tests" / "fixtures"
if str(_FIXTURES_DIR) not in sys.path:
    sys.path.insert(0, str(_FIXTURES_DIR))
from git_repo import init_local_git_repo  # noqa: E402


# --------------------------------------------------------------------------- #
# 辅助函数与常量
# --------------------------------------------------------------------------- #

_TEMPLATES_DIR = _PROJECT_ROOT / "templates" / "git_coordination"
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test Bot",
    "GIT_AUTHOR_EMAIL": "bot@example.test",
    "GIT_COMMITTER_NAME": "Test Bot",
    "GIT_COMMITTER_EMAIL": "bot@example.test",
    "GIT_TERMINAL_PROMPT": "0",
}


def _run(coro: Any) -> Any:
    """在独立事件循环中执行协程并返回结果（与现有集成测试风格一致）。"""
    return asyncio.run(coro)


def _git(repo: Path, *args: str) -> str:
    """同步执行 git 命令，返回 stdout（用于 fixture 准备与断言）。

    直接用 ``subprocess.run`` 而非 GitCli：测试 fixture 不需要白名单/路径限制
    等安全保证，且需要在仓库外读取（如 ``git -C``）。
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
    """使用 ``init_local_git_repo`` 初始化一个真实 git 仓库（含 main 与初始提交）。

    遵循任务要求：使用 ``tests/fixtures/git_repo.py`` 的工厂创建仓库。
    """
    repo_path = tmp_path / "repo"
    return init_local_git_repo(repo_path).path


@pytest.fixture()
def git_cli(git_repo: Path) -> SubprocessGitCli:
    """绑定到 ``git_repo`` 的 SubprocessGitCli。"""
    return SubprocessGitCli(allowed_roots=[git_repo])


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
# 验收 1：快照包含 control commit、任务和节点
# --------------------------------------------------------------------------- #


class TestSnapshotContainsControlFields:
    """``fetch_control`` 返回的快照含完整字段。"""

    def test_snapshot_has_required_fields(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """快照含 control_commit、tasks_paths、nodes_paths、events_paths 等字段。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-snap-001")

        snapshot = _run(service.fetch_control("proj-snap-001"))

        # 验收 1：快照含 control_commit（40 字符 SHA-1）。
        assert isinstance(snapshot["control_commit"], str)
        assert len(snapshot["control_commit"]) == 40
        int(snapshot["control_commit"], 16)  # 是 hex

        # 验收 1：快照含 project_id。
        assert snapshot["project_id"] == "proj-snap-001"

        # 验收 1：快照含 commit_timestamp（ISO 8601）。
        assert isinstance(snapshot["commit_timestamp"], str)
        assert snapshot["commit_timestamp"]  # 非空

        # 验收 1：快照含 project_yaml（dict）。
        assert isinstance(snapshot["project_yaml"], dict)
        assert snapshot["project_yaml"]["project_id"] == "proj-snap-001"
        assert snapshot["project_yaml"]["schema_version"] == 1
        assert snapshot["project_yaml"]["control_branch"] == "maf/control"
        assert snapshot["project_yaml"]["coordination_mode"] == "git_single_writer"

        # 验收 1：快照含 status_md（文本）。
        assert isinstance(snapshot["status_md"], str)

        # 验收 1：快照含 tasks_paths / nodes_paths / events_paths（列表）。
        assert isinstance(snapshot["tasks_paths"], list)
        assert isinstance(snapshot["nodes_paths"], list)
        assert isinstance(snapshot["events_paths"], list)
        # 初始化后 tasks/nodes/events 目录通过 .gitkeep 跟踪。
        assert ".maf/tasks/.gitkeep" in snapshot["tasks_paths"]
        assert ".maf/nodes/.gitkeep" in snapshot["nodes_paths"]
        assert ".maf/events/.gitkeep" in snapshot["events_paths"]

        # 验收 1：快照含 tasks / nodes 占位列表（TASK-016 仅占位）。
        assert snapshot["tasks"] == []
        assert snapshot["nodes"] == []

        # 验收 1：快照含 generated_at（ISO 8601）。
        assert isinstance(snapshot["generated_at"], str)
        assert snapshot["generated_at"]  # 非空

    def test_snapshot_parses_and_validates_task_and_node_documents(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """Control 快照包含已提交的 task/node 对象，而不是只返回路径。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-objects-001")
        _git(git_repo, "switch", "maf/control")
        (git_repo / ".maf" / "tasks" / "TASK-001.yaml").write_text(
            "schema_version: 1\n"
            "task_id: TASK-001\n"
            "title: First task\n"
            "status: READY\n"
            "requirements: {}\n"
            "dependencies: []\n"
            "assignment: null\n"
            "progress: {}\n"
            "delivery: {}\n"
            "version: 1\n",
            encoding="utf-8",
        )
        (git_repo / ".maf" / "nodes" / "node-aaaaaaaa.yaml").write_text(
            "schema_version: 1\n"
            "node_id: node-aaaaaaaa\n"
            "display_name: Test node\n"
            "git_identity:\n  name: Test Bot\n  email: bot@example.test\n"
            "capabilities: []\n"
            "capacity: 1\n"
            "status: ACTIVE\n"
            "version: 1\n",
            encoding="utf-8",
        )
        _git(git_repo, "add", ".maf/tasks/TASK-001.yaml", ".maf/nodes/node-aaaaaaaa.yaml")
        _git(git_repo, "commit", "-m", "test: add control objects")
        _git(git_repo, "switch", "main")

        snapshot = _run(service.fetch_control("proj-objects-001"))
        assert [task["task_id"] for task in snapshot["tasks"]] == ["TASK-001"]
        assert [node["node_id"] for node in snapshot["nodes"]] == ["node-aaaaaaaa"]

    def test_control_commit_matches_branch_head(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """快照的 control_commit 与 ``git rev-parse maf/control`` 一致。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-match-001")

        snapshot = _run(service.fetch_control("proj-match-001"))

        actual_head = _git(git_repo, "rev-parse", "maf/control")
        assert snapshot["control_commit"] == actual_head

    def test_project_yaml_reflects_control_branch_content(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """快照的 project_yaml 与 control 分支上 .maf/project.yaml 一致。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-yaml-001")

        snapshot = _run(service.fetch_control("proj-yaml-001"))

        # 直接从 control 分支读取 project.yaml 做比对。
        text = _git(git_repo, "show", "maf/control:.maf/project.yaml")
        expected = yaml.safe_load(text)
        assert snapshot["project_yaml"] == expected

    def test_status_md_reflects_control_branch_content(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """快照的 status_md 与 control 分支上 .maf/status.md 一致。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-status-001")

        snapshot = _run(service.fetch_control("proj-status-001"))

        # 直接从 control 分支读取 status.md 做比对。
        # 注意：``_git`` 会 strip 首尾空白，而 snapshot 保留 git show 的原始输出
        # （含尾部换行），因此对两侧做 rstrip 后比较语义内容。
        expected = _git(git_repo, "show", "maf/control:.maf/status.md")
        assert snapshot["status_md"].rstrip() == expected.rstrip()

    def test_tasks_nodes_events_paths_listed_correctly(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """快照的 tasks/nodes/events 路径列表与 ``git ls-tree`` 一致。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-paths-001")

        snapshot = _run(service.fetch_control("proj-paths-001"))

        # 直接 ls-tree 获取预期列表。
        expected_tasks = sorted(
            line.strip()
            for line in _git(
                git_repo,
                "ls-tree",
                "-r",
                "--name-only",
                "maf/control",
                "--",
                ".maf/tasks/",
            ).splitlines()
            if line.strip()
        )
        expected_nodes = sorted(
            line.strip()
            for line in _git(
                git_repo,
                "ls-tree",
                "-r",
                "--name-only",
                "maf/control",
                "--",
                ".maf/nodes/",
            ).splitlines()
            if line.strip()
        )
        expected_events = sorted(
            line.strip()
            for line in _git(
                git_repo,
                "ls-tree",
                "-r",
                "--name-only",
                "maf/control",
                "--",
                ".maf/events/",
            ).splitlines()
            if line.strip()
        )
        assert snapshot["tasks_paths"] == expected_tasks
        assert snapshot["nodes_paths"] == expected_nodes
        assert snapshot["events_paths"] == expected_events


# --------------------------------------------------------------------------- #
# 验收 1（续）：commit 去重
# --------------------------------------------------------------------------- #


class TestCommitDeduplication:
    """相同 commit 的重复调用返回相同 ``control_commit``，调用方可据此跳过。"""

    def test_repeat_call_returns_same_commit(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """两次 ``fetch_control`` 返回相同 ``control_commit``。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-dedup-001")

        first = _run(service.fetch_control("proj-dedup-001"))
        second = _run(service.fetch_control("proj-dedup-001"))

        assert first["control_commit"] == second["control_commit"]
        # project_yaml 内容也应一致。
        assert first["project_yaml"] == second["project_yaml"]

    def test_dedup_identifiable_via_control_commit(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """调用方可通过 ``control_commit`` 判断"无变化"。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-dedup-002")

        snapshot = _run(service.fetch_control("proj-dedup-002"))
        # 模拟调用方据 control_commit 跳过处理。
        known_commit = snapshot["control_commit"]

        # 再次调用，commit 未变。
        snapshot2 = _run(service.fetch_control("proj-dedup-002"))
        assert snapshot2["control_commit"] == known_commit
        # 调用方可据此跳过重复处理。


# --------------------------------------------------------------------------- #
# 验收 2：Schema 错误或 project_id 不一致时不返回部分快照
# --------------------------------------------------------------------------- #


class TestNoPartialSnapshotOnErrors:
    """任一步骤失败时抛异常，不返回部分快照。"""

    def test_empty_project_id_raises_argument_error(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``project_id`` 为空抛 ArgumentError。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-err-001")

        with pytest.raises(ArgumentError, match="project_id"):
            _run(service.fetch_control(""))

    def test_project_id_mismatch_raises_argument_error(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``project_id`` 与 control 上 ``.maf/project.yaml`` 不一致抛 ArgumentError。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-real-001")

        with pytest.raises(ArgumentError, match="mismatch"):
            _run(service.fetch_control("proj-wrong-001"))

    def test_control_branch_missing_raises_unsupported(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``maf/control`` 分支不存在抛 UnsupportedOperationError。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        # 不调用 initialize_project，control 分支不存在。
        with pytest.raises(UnsupportedOperationError) as exc_info:
            _run(service.fetch_control("proj-missing-001"))
        assert exc_info.value.context["reason"] == "control_branch_missing"

    def test_invalid_project_yaml_raises_validation_error(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``.maf/project.yaml`` 不符合 Schema 时抛 ValidationError，不返回部分快照。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-schema-001")

        # 在 maf/control 上手动写入一个 Schema 不合法的 project.yaml
        # （缺少 required 字段 default_branch）。
        _git(git_repo, "switch", "maf/control")
        (git_repo / ".maf" / "project.yaml").write_text(
            "schema_version: 1\n"
            "project_id: proj-schema-001\n"
            "control_branch: maf/control\n"
            # 故意缺少 default_branch
            "coordination_mode: git_single_writer\n"
            "task_schema: task-v1\n"
            "node_schema: node-v1\n"
            "event_schema: event-v1\n",
            encoding="utf-8",
        )
        _git(git_repo, "add", ".maf/project.yaml")
        _git(git_repo, "commit", "-m", "test: invalid project.yaml")
        _git(git_repo, "switch", "main")

        with pytest.raises(ValidationError):
            _run(service.fetch_control("proj-schema-001"))

    def test_missing_project_yaml_raises_unsupported(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``.maf/project.yaml`` 缺失时抛 UnsupportedOperationError，不返回部分快照。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-missing-yaml-001")

        # 在 maf/control 上删除 project.yaml。
        _git(git_repo, "switch", "maf/control")
        _git(git_repo, "rm", ".maf/project.yaml")
        _git(git_repo, "commit", "-m", "test: remove project.yaml")
        _git(git_repo, "switch", "main")

        with pytest.raises(UnsupportedOperationError) as exc_info:
            _run(service.fetch_control("proj-missing-yaml-001"))
        # 错误上下文标记读取失败。
        assert exc_info.value.context["reason"] == "read_failed"

    def test_missing_status_md_raises_unsupported(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``.maf/status.md`` 缺失时抛 UnsupportedOperationError，不返回部分快照。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-missing-status-001")

        # 在 maf/control 上删除 status.md。
        _git(git_repo, "switch", "maf/control")
        _git(git_repo, "rm", ".maf/status.md")
        _git(git_repo, "commit", "-m", "test: remove status.md")
        _git(git_repo, "switch", "main")

        with pytest.raises(UnsupportedOperationError) as exc_info:
            _run(service.fetch_control("proj-missing-status-001"))
        assert exc_info.value.context["reason"] == "read_failed"


# --------------------------------------------------------------------------- #
# 验收 3：读取不改变工作区文件
# --------------------------------------------------------------------------- #


class TestReadOnlyNoSideEffects:
    """``fetch_control`` 只读，不修改工作区、不切换分支、不新增 commit。"""

    def test_fetch_control_does_not_change_control_commit(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``fetch_control`` 调用前后 control 分支 commit 不变。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-readonly-001")

        commit_before = _git(git_repo, "rev-parse", "maf/control")
        _run(service.fetch_control("proj-readonly-001"))
        commit_after = _git(git_repo, "rev-parse", "maf/control")

        assert commit_before == commit_after

    def test_fetch_control_does_not_change_main_commit(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``fetch_control`` 调用前后 main 分支 commit 不变。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-readonly-002")

        main_before = _git(git_repo, "rev-parse", "main")
        _run(service.fetch_control("proj-readonly-002"))
        main_after = _git(git_repo, "rev-parse", "main")

        assert main_before == main_after

    def test_fetch_control_does_not_switch_working_tree(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``fetch_control`` 后工作树仍在 main 分支。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-readonly-003")

        _run(service.fetch_control("proj-readonly-003"))

        current = _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD")
        assert current == "main"

    def test_fetch_control_does_not_create_maf_on_main(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``fetch_control`` 后 main 工作树无 ``.maf/`` 痕迹。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-readonly-004")

        _run(service.fetch_control("proj-readonly-004"))

        assert not (git_repo / ".maf").exists()

    def test_fetch_control_does_not_modify_business_files(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``fetch_control`` 不修改 main 上的业务文件。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-readonly-005")

        # 读取 README.md 内容（init_local_git_repo 写入的）。
        readme_before = (git_repo / "README.md").read_text(encoding="utf-8")
        _run(service.fetch_control("proj-readonly-005"))
        readme_after = (git_repo / "README.md").read_text(encoding="utf-8")

        assert readme_before == readme_after

    def test_fetch_control_does_not_create_new_commits(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``fetch_control`` 不新增任何 commit（control 和 main 的 commit 数不变）。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-readonly-006")

        control_count_before = int(
            _git(git_repo, "rev-list", "--count", "maf/control")
        )
        main_count_before = int(_git(git_repo, "rev-list", "--count", "main"))

        _run(service.fetch_control("proj-readonly-006"))
        _run(service.fetch_control("proj-readonly-006"))  # 调用两次确保幂等

        control_count_after = int(
            _git(git_repo, "rev-list", "--count", "maf/control")
        )
        main_count_after = int(_git(git_repo, "rev-list", "--count", "main"))

        assert control_count_after == control_count_before
        assert main_count_after == main_count_before

    def test_fetch_control_preserves_untracked_files(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``fetch_control`` 不影响 main 工作树上的未跟踪文件。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-readonly-007")

        # 在 main 工作树上添加未跟踪文件。
        scratch = git_repo / "scratch.txt"
        scratch.write_text("temporary", encoding="utf-8")

        _run(service.fetch_control("proj-readonly-007"))

        # 未跟踪文件仍在。
        assert scratch.exists()
        assert scratch.read_text(encoding="utf-8") == "temporary"
        # 工作树仍在 main。
        assert _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"


# --------------------------------------------------------------------------- #
# 验收 1（续）：CoordinationSnapshot 类型与契约一致性
# --------------------------------------------------------------------------- #


class TestCoordinationSnapshotContract:
    """``CoordinationSnapshot`` 字段与契约定义一致。"""

    def test_snapshot_is_dict_with_expected_keys(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """快照是 dict 且包含 ``CoordinationSnapshot`` 全部键。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-contract-001")

        snapshot: CoordinationSnapshot = _run(
            service.fetch_control("proj-contract-001")
        )

        expected_keys = {
            "project_id",
            "control_commit",
            "commit_timestamp",
            "project_yaml",
            "status_md",
            "tasks_paths",
            "nodes_paths",
            "events_paths",
            "tasks",
            "nodes",
            "generated_at",
        }
        assert set(snapshot.keys()) == expected_keys

    def test_snapshot_project_yaml_passes_schema(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """快照中的 ``project_yaml`` 通过 ``project-v1`` Schema 校验。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _init_control(service, project_id="proj-contract-002")

        snapshot = _run(service.fetch_control("proj-contract-002"))

        # 不抛异常即通过。
        schema_loader.validate(
            SchemaRef(name="project", version=ProtocolVersion.latest().value),
            snapshot["project_yaml"],
        )
        assert snapshot["project_yaml"]["schema_version"] == (
            ProtocolVersion.latest().value
        )


# --------------------------------------------------------------------------- #
# Runner 端：RunnerGitClient.fetch_control 集成测试
# --------------------------------------------------------------------------- #


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


@pytest.fixture()
def runner_env(
    tmp_path: Path,
    templates_dir: Path,
    schema_loader: SchemaLoader,
) -> tuple[Path, Path, RunnerGitClient, str]:
    """创建远端 + 本地 clone + 在本地初始化 control + push 到远端 + RunnerGitClient。

    返回 (remote_path, local_path, client, project_id)。
    """
    remote, local = _setup_remote_and_local(tmp_path)

    # 在本地 clone 上用 LocalGitCoordinationService 初始化 control 分支。
    local_cli = SubprocessGitCli(allowed_roots=[local])
    service = LocalGitCoordinationService(
        git_cli=local_cli,
        repository_path=str(local),
        templates_dir=templates_dir,
        schema_loader=schema_loader,
    )
    project_id = "proj-runner-001"
    _run(service.initialize_project("binding-runner", project_id))

    # 把 maf/control push 到远端。
    subprocess.run(
        ["git", "-C", str(local), "push", "-q", "origin", "maf/control"],
        check=True,
        env=_GIT_ENV,
    )

    # 构造 RunnerGitClient，绑定到本地 clone。
    runner_cli = RunnerGitCli(allowed_roots=[tmp_path])
    client = RunnerGitClient(
        git_cli=runner_cli,
        repository_path=str(local),
        control_remote="origin",
        control_branch="maf/control",
        node_id="node-runner-test",
    )
    return remote, local, client, project_id


class TestRunnerFetchControlSnapshot:
    """``RunnerGitClient.fetch_control`` 返回 fetch 状态与快照字段。"""

    def test_fetch_ok_and_control_commit_present(
        self,
        runner_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """fetch 成功后 ``fetch_ok=True`` 且 ``control_commit`` 非空。"""
        _remote, _local, client, _project_id = runner_env
        result = _run(client.fetch_control())

        assert result["fetch_ok"] is True
        assert result["control_commit"] is not None
        assert len(result["control_commit"]) == 40
        assert result["control_branch"] == "maf/control"
        assert result["remote"] == "origin"
        assert result["fetch_error"] is None

    def test_snapshot_fields_present(
        self,
        runner_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """fetch 成功后快照字段（project_id、project_yaml、status_md 等）存在。"""
        _remote, _local, client, project_id = runner_env
        result = _run(client.fetch_control())

        # 快照字段。
        assert result["project_id"] == project_id
        assert isinstance(result["project_yaml"], dict)
        assert result["project_yaml"]["project_id"] == project_id
        assert isinstance(result["status_md"], str)
        assert isinstance(result["tasks_paths"], list)
        assert isinstance(result["nodes_paths"], list)
        assert isinstance(result["events_paths"], list)
        assert ".maf/tasks/.gitkeep" in result["tasks_paths"]
        assert isinstance(result["commit_timestamp"], str)
        assert isinstance(result["generated_at"], str)
        # 没有 snapshot_error。
        assert "snapshot_error" not in result

    def test_control_commit_matches_remote(
        self,
        runner_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """``control_commit`` 与远端 ``maf/control`` HEAD 一致。"""
        remote, _local, client, _project_id = runner_env
        result = _run(client.fetch_control())

        remote_head = _git(remote, "rev-parse", "refs/heads/maf/control")
        assert result["control_commit"] == remote_head

    def test_dedup_via_control_commit(
        self,
        runner_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """两次 fetch 返回相同 ``control_commit``，调用方可据此去重。"""
        _remote, _local, client, _project_id = runner_env
        first = _run(client.fetch_control())
        second = _run(client.fetch_control())

        assert first["control_commit"] == second["control_commit"]
        assert first["project_id"] == second["project_id"]

    def test_fetch_failure_returns_fetch_error(
        self,
        tmp_path: Path,
    ) -> None:
        """fetch 失败时 ``fetch_ok=False``、``control_commit=None``、``fetch_error`` 非空。"""
        # 创建一个本地仓库但没有远端配置，fetch 会失败。
        local = tmp_path / "lonely"
        init_local_git_repo(local)

        cli = RunnerGitCli(allowed_roots=[tmp_path])
        client = RunnerGitClient(
            git_cli=cli,
            repository_path=str(local),
            control_remote="origin",
            control_branch="maf/control",
        )
        result = _run(client.fetch_control())

        assert result["fetch_ok"] is False
        assert result["control_commit"] is None
        assert result["fetch_error"] is not None
        # fetch 失败时不应有快照字段。
        assert "project_yaml" not in result

    def test_fetch_control_is_read_only(
        self,
        runner_env: tuple[Path, Path, RunnerGitClient, str],
    ) -> None:
        """``fetch_control`` 不修改本地工作树、不新增 commit。"""
        _remote, local, client, _project_id = runner_env

        local_head_before = _git(local, "rev-parse", "HEAD")
        current_branch_before = _git(
            local, "rev-parse", "--abbrev-ref", "HEAD"
        )

        _run(client.fetch_control())

        local_head_after = _git(local, "rev-parse", "HEAD")
        current_branch_after = _git(
            local, "rev-parse", "--abbrev-ref", "HEAD"
        )

        assert local_head_before == local_head_after
        assert current_branch_before == current_branch_after
