"""TASK-015 集成测试：初始化 maf/control 分支。

验收标准覆盖（对应 TASK-015 文档）：

1. **空仓库可幂等初始化**：``initialize_project`` 在无 ``maf/control`` 的仓库上
   创建分支、写入 ``.maf/``，返回 commit；再次调用不破坏已有 control，返回
   相同 commit。
2. **不兼容现有协议会停止而非覆盖**：当 ``maf/control`` 上 ``.maf/project.yaml``
   的 ``schema_version`` / ``control_branch`` / ``coordination_mode`` 与协议常量
   不一致时，``initialize_project`` 抛 :class:`UnsupportedOperationError`，
   **不覆盖**已有协议。
3. **初始化不修改 main 业务文件**：``main`` 工作树无 ``.maf/`` 痕迹，业务文件
   内容不变。
4. **project.yaml 字段正确**：``schema_version``、``control_branch``、
   ``coordination_mode``、``task_schema``、``node_schema``、``event_schema``、
   ``progress_interval_minutes``、``assignment_timeout_minutes``、
   ``assignment_grace_minutes`` 与模板一致；``project_id`` 替换为真实值。
5. **与 Schema 校验一致**：写入的 ``project.yaml`` 通过 ``project-v1`` Schema。
6. **TASK-022 状态转换未破坏**：``LocalGitCoordinationService.state_service``
   仍可正确处理合法/非法转换（与 ``test_task_states.py`` 等价校验）。

测试使用真实 ``git init`` 创建临时仓库，让 ``SubprocessGitCli`` 执行真实 git
命令（集成测试），覆盖分支创建、commit、``git show <branch>:<path>`` 读取等
真实行为。
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
)
from maf_domain.states import (  # noqa: E402
    Actor,
    TaskEvent,
    TaskState,
)
from maf_repository_adapters import SubprocessGitCli  # noqa: E402
from maf_server.git_coordination.schemas import SchemaLoader  # noqa: E402
from maf_server.modules.git_coordination.service import (  # noqa: E402
    GitCoordinationStateService,
    LocalGitCoordinationService,
)


# --------------------------------------------------------------------------- #
# 辅助函数与 fixtures
# --------------------------------------------------------------------------- #


def _run(coro: Any) -> Any:
    """在独立事件循环中执行协程并返回结果（与现有集成测试风格一致）。"""
    return asyncio.run(coro)


def _git(repo: Path, *args: str) -> str:
    """同步执行 git 命令，返回 stdout（用于 fixture 准备与断言）。

    直接用 ``subprocess.run`` 而非 GitCli：测试 fixture 不需要白名单/路径限制
    等安全保证，且需要在仓库外读取（如 ``git -C``）。
    """
    env = os.environ.copy()
    # 防止 Windows 上 git 打开交互式凭证提示。
    env["GIT_TERMINAL_PROMPT"] = "0"
    # 测试提交需要稳定的 author 身份。
    env.setdefault("GIT_AUTHOR_NAME", "maf-test")
    env.setdefault("GIT_AUTHOR_EMAIL", "maf-test@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "maf-test")
    env.setdefault("GIT_COMMITTER_EMAIL", "maf-test@example.com")
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=env,
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


@pytest.fixture()
def templates_dir() -> Path:
    """返回项目内置的 ``templates/git_coordination``。"""
    return (
        Path(__file__).resolve().parents[2]
        / "templates"
        / "git_coordination"
    )


@pytest.fixture()
def schema_loader() -> SchemaLoader:
    """使用默认 ``templates/git_coordination/schemas/`` 的 SchemaLoader。"""
    return SchemaLoader()


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """初始化一个临时 git 仓库，含 main 分支和一个业务文件。

    模拟真实生产场景：仓库已有 main 分支和业务代码，``initialize_project``
    在此基础上创建 ``maf/control``，不修改 main。
    """
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    # 写入业务文件，模拟 main 上的真实代码。
    business_file = repo / "README.md"
    business_file.write_text("# business\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "main: initial business")
    return repo


@pytest.fixture()
def empty_repo(tmp_path: Path) -> Path:
    """完全空的 git 仓库（无提交、unborn main）。

    覆盖"空仓库可幂等初始化"验收标准的边界情况：``git branch`` 在 unborn HEAD
    上仍可创建分支。
    """
    repo = tmp_path / "empty"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    return repo


@pytest.fixture()
def git_cli(git_repo: Path) -> SubprocessGitCli:
    """绑定到 ``git_repo`` 的 SubprocessGitCli。"""
    return SubprocessGitCli(allowed_roots=[git_repo])


@pytest.fixture()
def git_cli_empty(empty_repo: Path) -> SubprocessGitCli:
    """绑定到 ``empty_repo`` 的 SubprocessGitCli。"""
    return SubprocessGitCli(allowed_roots=[empty_repo])


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


# --------------------------------------------------------------------------- #
# 验收 1：空仓库可幂等初始化
# --------------------------------------------------------------------------- #


class TestInitializeCreatesControlBranch:
    """``initialize_project`` 在没有 ``maf/control`` 的仓库上创建分支。"""

    def test_creates_control_branch_and_returns_commit(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """调用后存在 maf/control 分支，返回 40 字符 commit hash。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )

        commit = _run(
            service.initialize_project("binding-1", "proj-test-001")
        )

        assert isinstance(commit, str)
        assert len(commit) == 40, f"expected 40-char SHA-1, got {commit!r}"
        # hex string
        int(commit, 16)  # raises if not hex

        # maf/control 分支已存在。
        branches = _git(git_repo, "branch", "--list")
        assert "maf/control" in branches

    def test_writes_maf_directory_on_control_branch(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``maf/control`` 分支上含完整的 .maf/ 目录结构。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )

        _run(service.initialize_project("binding-1", "proj-test-001"))

        # 列出 maf/control 上的文件（不切换工作树）。
        tree = _git(git_repo, "ls-tree", "-r", "--name-only", "maf/control")
        # 必须包含 .maf/ 关键文件。
        assert ".maf/project.yaml" in tree
        assert ".maf/status.md" in tree
        assert ".maf/PROTOCOL.md" in tree
        assert ".maf/schemas/project-v1.schema.json" in tree
        assert ".maf/schemas/task-v1.schema.json" in tree
        assert ".maf/schemas/node-v1.schema.json" in tree
        assert ".maf/schemas/event-v1.schema.json" in tree
        # 空目录通过 .gitkeep 跟踪。
        assert ".maf/tasks/.gitkeep" in tree
        assert ".maf/nodes/.gitkeep" in tree
        assert ".maf/events/.gitkeep" in tree

    def test_initial_commit_has_fixed_message(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """初始 commit 使用固定 message，便于审计比对。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )

        _run(service.initialize_project("binding-1", "proj-test-001"))

        log = _git(git_repo, "log", "-1", "--pretty=%B", "maf/control")
        assert "maf: initialize control branch" in log

    def test_initialize_on_empty_repo(
        self,
        empty_repo: Path,
        git_cli_empty: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """空仓库（unborn main）也能初始化 control 分支。"""
        service = _make_service(
            git_cli=git_cli_empty,
            repository_path=str(empty_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )

        commit = _run(
            service.initialize_project("binding-1", "proj-empty-001")
        )

        assert len(commit) == 40
        branches = _git(empty_repo, "branch", "--list")
        assert "maf/control" in branches
        # project.yaml 被写入。
        tree = _git(empty_repo, "ls-tree", "-r", "--name-only", "maf/control")
        assert ".maf/project.yaml" in tree


# --------------------------------------------------------------------------- #
# 验收 1（续）：幂等
# --------------------------------------------------------------------------- #


class TestInitializeIdempotent:
    """重复调用 ``initialize_project`` 不破坏已有 control。"""

    def test_repeat_call_returns_same_commit(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """第二次调用返回相同 commit，且不新增 commit。

        注意：control 从 main 创建，所以包含 main 的所有历史 commit
        加上 init commit。本测试只关心"第二次调用不新增 commit"，
        而非固定 count=1。
        """
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )

        first = _run(service.initialize_project("b1", "proj-1"))
        count_after_first = int(
            _git(git_repo, "rev-list", "--count", "maf/control")
        )
        # 第二次：control 已存在且兼容 → 应幂等返回。
        second = _run(service.initialize_project("b1", "proj-1"))

        assert first == second
        # 第二次调用后 commit 数不变（幂等未新增 commit）。
        count_after_second = int(
            _git(git_repo, "rev-list", "--count", "maf/control")
        )
        assert count_after_second == count_after_first

    def test_repeat_call_with_different_project_id_still_idempotent(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """幂等校验基于协议兼容性，project_id 不影响兼容判定。

        已有 control 时，即使传入不同 project_id 也返回当前 HEAD（不覆盖）。
        这是有意为之：control 一旦初始化，``project_id`` 不可通过重新调用改写。
        如需变更 project_id，需要人工删除 control 分支或提交 CodeStructureChangeRequest。
        """
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )

        first = _run(service.initialize_project("b1", "proj-original"))
        second = _run(service.initialize_project("b1", "proj-different"))

        assert first == second
        # project.yaml 仍是原始值。
        project_yaml = _git(
            git_repo, "show", "maf/control:.maf/project.yaml"
        )
        assert "proj-original" in project_yaml
        assert "proj-different" not in project_yaml

    def test_idempotent_check_does_not_switch_working_tree(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """幂等校验通过 ``git show <branch>:<path>``，不切换工作树。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )

        _run(service.initialize_project("b1", "proj-1"))
        # 在 main 工作树上添加未跟踪文件，验证幂等调用不破坏工作树。
        (git_repo / "scratch.txt").write_text("temp", encoding="utf-8")
        # 再次调用。
        _run(service.initialize_project("b1", "proj-1"))
        # 工作树仍在 main 分支。
        current = _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD")
        assert current == "main"
        # 未跟踪文件未被提交（不在 maf/control 的 tree 中）。
        tree = _git(git_repo, "ls-tree", "-r", "--name-only", "maf/control")
        assert "scratch.txt" not in tree


# --------------------------------------------------------------------------- #
# 验收 2：不兼容协议会停止而非覆盖
# --------------------------------------------------------------------------- #


class TestIncompatibleProtocolStops:
    """已有 maf/control 但协议不兼容时，initialize 抛错，不覆盖。"""

    def _setup_incompatible_control(
        self,
        git_repo: Path,
        *,
        project_yaml: str,
    ) -> None:
        """在 maf/control 上手动写入一个不兼容的 .maf/project.yaml。"""
        # 切到 maf/control，写入文件，commit，切回 main。
        _git(git_repo, "switch", "maf/control")
        maf_dir = git_repo / ".maf"
        maf_dir.mkdir(exist_ok=True)
        (maf_dir / "project.yaml").write_text(project_yaml, encoding="utf-8")
        _git(git_repo, "add", ".maf/project.yaml")
        _git(git_repo, "commit", "-m", "test: incompatible protocol")
        _git(git_repo, "switch", "main")

    def test_wrong_schema_version_raises(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """schema_version=99（不兼容）抛 UnsupportedOperationError。"""
        # 先创建一个合法 control。
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        original_commit = _run(
            service.initialize_project("b1", "proj-1")
        )
        # 写入不兼容版本。
        self._setup_incompatible_control(
            git_repo,
            project_yaml=(
                "schema_version: 99\n"
                "project_id: proj-1\n"
                "control_branch: maf/control\n"
                "default_branch: main\n"
                "coordination_mode: git_single_writer\n"
                "task_schema: task-v1\n"
                "node_schema: node-v1\n"
                "event_schema: event-v1\n"
            ),
        )

        with pytest.raises(UnsupportedOperationError) as exc_info:
            _run(service.initialize_project("b1", "proj-1"))
        err = exc_info.value
        assert err.context["reason"] == "incompatible_protocol"
        assert any("schema_version" in i for i in err.context["issues"])

        # control 分支 commit 没有被覆盖（仍是人工写入的不兼容版本）。
        current = _git(git_repo, "rev-parse", "maf/control")
        assert current != original_commit

    def test_wrong_coordination_mode_raises(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """coordination_mode 不等于 git_single_writer 抛错。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _run(service.initialize_project("b1", "proj-1"))
        self._setup_incompatible_control(
            git_repo,
            project_yaml=(
                "schema_version: 1\n"
                "project_id: proj-1\n"
                "control_branch: maf/control\n"
                "default_branch: main\n"
                "coordination_mode: http_centralized\n"
                "task_schema: task-v1\n"
                "node_schema: node-v1\n"
                "event_schema: event-v1\n"
            ),
        )
        with pytest.raises(UnsupportedOperationError) as exc_info:
            _run(service.initialize_project("b1", "proj-1"))
        assert exc_info.value.context["reason"] == "incompatible_protocol"
        assert any("coordination_mode" in i for i in exc_info.value.context["issues"])

    def test_missing_project_yaml_raises(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """已有 maf/control 但无 .maf/project.yaml：抛错（不覆盖）。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        # 创建一个空的 maf/control（无 .maf/）。
        _git(git_repo, "branch", "maf/control", "main")
        with pytest.raises(UnsupportedOperationError) as exc_info:
            _run(service.initialize_project("b1", "proj-1"))
        assert exc_info.value.context["reason"] == "missing_project_yaml"


# --------------------------------------------------------------------------- #
# 验收 3：初始化不修改 main 业务文件
# --------------------------------------------------------------------------- #


class TestMainBusinessUntouched:
    """``initialize_project`` 不修改 main 上的业务代码。"""

    def test_main_branch_unchanged_after_init(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """main 分支 commit 在初始化前后相同。"""
        main_before = _git(git_repo, "rev-parse", "main")

        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _run(service.initialize_project("b1", "proj-1"))

        main_after = _git(git_repo, "rev-parse", "main")
        assert main_before == main_after

    def test_main_working_tree_has_no_maf_directory(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """main 工作树不留 .maf/ 痕迹（操作后切回 main）。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _run(service.initialize_project("b1", "proj-1"))

        # 工作树仍处于 main。
        current = _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD")
        assert current == "main"
        # main 工作树没有 .maf 目录。
        assert not (git_repo / ".maf").exists()
        # main 上的业务文件内容不变。
        assert (git_repo / "README.md").read_text(encoding="utf-8") == "# business\n"

    def test_initial_commit_only_touches_maf(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """control 的初始 commit 只动了 .maf/ 下的文件。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _run(service.initialize_project("b1", "proj-1"))

        # control HEAD 相对其父提交的 diff 应只列 .maf/ 下的文件。
        diff = _git(
            git_repo,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "maf/control",
        )
        for path in diff.splitlines():
            assert path.startswith(".maf/"), (
                f"unexpected file in initial control commit: {path!r}"
            )


# --------------------------------------------------------------------------- #
# 验收 4：project.yaml 字段正确
# --------------------------------------------------------------------------- #


class TestProjectYamlFields:
    """``.maf/project.yaml`` 字段与协议模板一致。"""

    def test_all_required_fields_present_and_correct(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """project.yaml 包含所有协议字段，且 control_branch/coordination_mode 等正确。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _run(service.initialize_project("b1", "proj-fields-001"))

        text = _git(git_repo, "show", "maf/control:.maf/project.yaml")
        data = yaml.safe_load(text)

        # 协议版本（验收 4：协议版本字段）
        assert data["schema_version"] == ProtocolVersion.latest().value == 1
        # control_branch（验收 4）
        assert data["control_branch"] == "maf/control"
        # coordination_mode（验收 4）
        assert data["coordination_mode"] == "git_single_writer"
        # default_branch（模板默认）
        assert data["default_branch"] == "main"
        # task/node/event Schema 引用
        assert data["task_schema"] == "task-v1"
        assert data["node_schema"] == "node-v1"
        assert data["event_schema"] == "event-v1"
        # 协议常量
        assert data["progress_interval_minutes"] == 15
        assert data["assignment_timeout_minutes"] == 60
        assert data["assignment_grace_minutes"] == 15

    def test_project_id_replaced_with_real_value(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``project_id`` 占位符被替换为真实值。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _run(service.initialize_project("b1", "proj-real-123"))

        text = _git(git_repo, "show", "maf/control:.maf/project.yaml")
        assert "replace-with-project-id" not in text
        assert "proj-real-123" in text

    def test_project_id_empty_rejected(
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
        with pytest.raises(ArgumentError):
            _run(service.initialize_project("b1", ""))

    def test_project_id_placeholder_rejected(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``project_id`` 等于模板占位符抛 ArgumentError。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        with pytest.raises(ArgumentError, match="placeholder"):
            _run(
                service.initialize_project("b1", "replace-with-project-id")
            )


# --------------------------------------------------------------------------- #
# 验收 5：与 Schema 校验一致
# --------------------------------------------------------------------------- #


class TestSchemaConsistency:
    """写入的 ``project.yaml`` 通过 ``project-v1`` Schema 校验。"""

    def test_written_project_yaml_passes_schema(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
        tmp_path: Path,
    ) -> None:
        """将 control 上的 project.yaml 拉到本地，用 SchemaLoader 校验通过。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _run(service.initialize_project("b1", "proj-schema-001"))

        # 通过 git show 把 project.yaml 拉到临时文件。
        text = _git(git_repo, "show", "maf/control:.maf/project.yaml")
        local_copy = tmp_path / "project.yaml"
        local_copy.write_text(text, encoding="utf-8")

        instance = yaml.safe_load(text)
        # 不抛异常即通过。
        schema_loader.validate_file(
            local_copy,
            SchemaRef(name="project", version=1),
        )
        assert instance["project_id"] == "proj-schema-001"

    def test_schema_loader_knows_project_v1(
        self,
        schema_loader: SchemaLoader,
    ) -> None:
        """SchemaLoader 加载了 project-v1。"""
        refs = schema_loader.known_refs()
        ref_names = {(r.name, r.version) for r in refs}
        assert ("project", 1) in ref_names


# --------------------------------------------------------------------------- #
# 验收 6：TASK-022 状态转换未破坏
# --------------------------------------------------------------------------- #


class TestTaskStateMachinePreserved:
    """``LocalGitCoordinationService.state_service`` 仍是 ``GitCoordinationStateService``。"""

    def test_state_service_is_git_coordination_state_service(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        assert isinstance(service.state_service, GitCoordinationStateService)

    def test_state_service_legal_transition_still_works(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """TASK-022 合法转换在 LocalGitCoordinationService 上仍然可用。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        result = service.state_service.apply_task_event(
            TaskState.PLANNED,
            TaskEvent.DEPENDENCIES_RESOLVED,
            actor=Actor.SCHEDULER,
            current_version=1,
        )
        assert result.new_state == TaskState.READY
        assert result.new_version == 2

    def test_state_service_illegal_transition_still_raises(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """TASK-022 非法转换仍被拒绝。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        with pytest.raises(UnsupportedOperationError):
            service.state_service.apply_task_event(
                TaskState.DONE, TaskEvent.PROGRESS_REPORTED
            )

    def test_state_service_node_actor_cannot_set_done(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """TASK-022 节点不能直接设置 DONE。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        with pytest.raises(UnsupportedOperationError):
            service.state_service.apply_task_event(
                TaskState.REVIEWING,
                TaskEvent.REVIEW_APPROVED,
                actor=Actor.NODE,
            )

    def test_initialize_does_not_break_state_service(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        templates_dir: Path,
        schema_loader: SchemaLoader,
    ) -> None:
        """``initialize_project`` 后状态机仍可用（同一实例）。"""
        service = _make_service(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=templates_dir,
            schema_loader=schema_loader,
        )
        _run(service.initialize_project("b1", "proj-1"))

        # 状态机仍正常工作。
        assert service.state_service.can_apply_task_event(
            TaskState.PLANNED, TaskEvent.DEPENDENCIES_RESOLVED
        )
        assert not service.state_service.can_apply_task_event(
            TaskState.DONE, TaskEvent.PROGRESS_REPORTED
        )


# --------------------------------------------------------------------------- #
# 模板缺失保护
# --------------------------------------------------------------------------- #


class TestTemplateMissingProtection:
    """模板缺失时 ``initialize_project`` 抛 ArgumentError，停止而非半写入。"""

    def test_missing_templates_dir_raises(
        self,
        git_repo: Path,
        git_cli: SubprocessGitCli,
        schema_loader: SchemaLoader,
        tmp_path: Path,
    ) -> None:
        """templates_dir 不存在时抛 ArgumentError。"""
        # 指向一个空目录作为 templates_dir。
        empty_templates = tmp_path / "empty_templates"
        empty_templates.mkdir()
        service = LocalGitCoordinationService(
            git_cli=git_cli,
            repository_path=str(git_repo),
            templates_dir=empty_templates,
            schema_loader=schema_loader,
        )
        with pytest.raises(ArgumentError, match="template missing"):
            _run(service.initialize_project("b1", "proj-1"))

        # 验证 main 工作树未受污染。
        assert not (git_repo / ".maf").exists()
        # 工作树仍在 main。
        current = _git(git_repo, "rev-parse", "--abbrev-ref", "HEAD")
        assert current == "main"
