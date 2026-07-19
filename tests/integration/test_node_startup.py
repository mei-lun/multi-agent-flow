"""TASK-066 集成测试：节点初始化与自检。

验收标准覆盖：

1. **自检失败不申请任务**：Docker/Git 不可用或安全基线检查失败时，
   :func:`run_startup` 返回 ``ok=False`` 且不构造注册事件。
2. **node_id 和 Git 身份稳定**：同一 ``NodeSettings`` 多次启动产生相同的
   ``node_id`` 与 Git 身份。
3. **能力清单只来自可信本地配置和实际探测**：``NodeManifest.capabilities``
   来自 ``NodeSettings.labels``，``payload.environment`` 来自
   ``EnvironmentInfoProvider`` 实际探测，不被远程任务修改。

测试使用 mock 探针避免依赖真实 Docker daemon，使用真实 git 验证仓库绑定检查。
所有异步入口经 ``asyncio.run`` 同步执行，与现有集成测试一致。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

# packages/artifact_schemas/src 不在 pyproject.toml 的 pythonpath 中，
# 显式添加以做 Schema 校验（与 test_node_identity.py 一致）。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT_SCHEMAS_SRC = _PROJECT_ROOT / "packages" / "artifact_schemas" / "src"
if str(_ARTIFACT_SCHEMAS_SRC) not in sys.path:
    sys.path.insert(0, str(_ARTIFACT_SCHEMAS_SRC))

from maf_artifact_schemas.protocol import SchemaRef  # noqa: E402
from maf_runner.config import NodeSettings  # noqa: E402
from maf_runner.main import run_startup  # noqa: E402
from maf_runner.registry import RunnerRegistry  # noqa: E402
from maf_runner.security.boundaries import (  # noqa: E402
    BaselineCheckResult,
    LocalSecurityBaseline,
)
from maf_runner.security.startup_check import (  # noqa: E402
    CheckResult,
    LocalDependencyProbe,
    StartupChecker,
)
from maf_server.git_coordination.schemas import SchemaLoader  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

_VALID_NODE_ID = "node-12345678-1234-1234-1234-123456789abc"
_VALID_CONTROL_COMMIT = "abcdef1234567890abcdef1234567890abcdef12"
_TEMPLATES_DIR = _PROJECT_ROOT / "templates" / "git_coordination"
_SCHEMAS_DIR = _TEMPLATES_DIR / "schemas"
_NODE_REF = SchemaRef(name="node", version=1)
_EVENT_REF = SchemaRef(name="event", version=1)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def loader() -> SchemaLoader:
    return SchemaLoader(_SCHEMAS_DIR)


@pytest.fixture()
def _clear_maf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除所有 ``MAF_*`` 环境变量，避免本地 .env 污染测试。"""
    for key in list(os.environ):
        if key.startswith("MAF_"):
            monkeypatch.delenv(key, raising=False)


# --------------------------------------------------------------------------- #
# mock 探针
# --------------------------------------------------------------------------- #


class _MockProbe:
    """可控依赖探针，返回预设的检查结果。"""

    def __init__(
        self,
        *,
        docker_ok: bool = True,
        git_ok: bool = True,
        repo_binding_ok: bool = True,
        docker_detail: str = "docker daemon reachable",
        git_detail: str = "git version 2.45.0",
        repo_detail: str = "valid git repository",
    ) -> None:
        self._docker_ok = docker_ok
        self._git_ok = git_ok
        self._repo_binding_ok = repo_binding_ok
        self._docker_detail = docker_detail
        self._git_detail = git_detail
        self._repo_detail = repo_detail

    def check_docker(self, docker_binary: str) -> CheckResult:
        if self._docker_ok:
            return CheckResult.pass_("docker_info", self._docker_detail)
        return CheckResult.fail("docker_info", self._docker_detail)

    def check_git(self, git_binary: str) -> CheckResult:
        if self._git_ok:
            return CheckResult.pass_("git_version", self._git_detail)
        return CheckResult.fail("git_version", self._git_detail)

    def check_repo_binding(
        self, workspace_root: Path, control_remote_url: str
    ) -> CheckResult:
        if self._repo_binding_ok:
            return CheckResult.pass_("repo_binding", self._repo_detail)
        return CheckResult.fail("repo_binding", self._repo_detail)


class _MockBaseline:
    """可控安全基线检查器。"""

    def __init__(
        self,
        *,
        workspace_ok: bool = True,
        docker_socket_ok: bool = True,
        not_root_ok: bool = True,
    ) -> None:
        self._workspace_ok = workspace_ok
        self._docker_socket_ok = docker_socket_ok
        self._not_root_ok = not_root_ok

    def check_workspace_writable(self, workspace_root: Path) -> BaselineCheckResult:
        if self._workspace_ok:
            return BaselineCheckResult.pass_(
                "workspace_writable", str(workspace_root)
            )
        return BaselineCheckResult.fail(
            "workspace_writable", f"{workspace_root} not writable"
        )

    def check_docker_socket(self, docker_socket: str) -> BaselineCheckResult:
        if self._docker_socket_ok:
            return BaselineCheckResult.pass_("docker_socket", docker_socket)
        return BaselineCheckResult.fail(
            "docker_socket", f"{docker_socket} not accessible"
        )

    def check_not_running_as_root(self) -> BaselineCheckResult:
        if self._not_root_ok:
            return BaselineCheckResult.pass_("not_root", "uid=1000")
        return BaselineCheckResult.fail("not_root", "running as root")


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #


def _make_settings(
    workspace_root: Path,
    *,
    node_id: str = _VALID_NODE_ID,
    control_remote_url: str = "origin",
    **overrides: Any,
) -> NodeSettings:
    """构造测试用 ``NodeSettings``。"""
    kwargs: dict[str, Any] = dict(
        node_id=node_id,
        control_remote_url=control_remote_url,
        workspace_root=workspace_root,
        model_mapping_path=workspace_root / "model-mapping.yaml",
        capability_token_cache_path=Path("capability-tokens.db"),
        _env_file=None,
    )
    kwargs.update(overrides)
    return NodeSettings(**kwargs)


def _make_checker(
    *,
    docker_ok: bool = True,
    git_ok: bool = True,
    repo_binding_ok: bool = True,
    workspace_ok: bool = True,
    docker_socket_ok: bool = True,
    not_root_ok: bool = True,
) -> StartupChecker:
    """构造带 mock 探针的 ``StartupChecker``。"""
    return StartupChecker(
        probe=_MockProbe(
            docker_ok=docker_ok,
            git_ok=git_ok,
            repo_binding_ok=repo_binding_ok,
        ),
        baseline=_MockBaseline(
            workspace_ok=workspace_ok,
            docker_socket_ok=docker_socket_ok,
            not_root_ok=not_root_ok,
        ),
    )


# --------------------------------------------------------------------------- #
# 验收 1：自检失败不申请任务
# --------------------------------------------------------------------------- #


class TestSelfCheckFailureNoRegistration:
    """自检失败时不构造注册事件，不申请任务。"""

    def test_docker_unavailable_no_event(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        settings = _make_settings(tmp_path)
        checker = _make_checker(docker_ok=False)
        outcome = run_startup(
            settings,
            checker=checker,
            control_commit=_VALID_CONTROL_COMMIT,
            output_json=False,
        )
        assert outcome.ok is False
        assert outcome.event is None
        assert outcome.manifest is None
        assert outcome.check_result is not None
        assert outcome.check_result.ok is False
        # 失败报告中包含 docker_info 失败信息。
        report = outcome.check_result.summary()
        assert "docker_info" in report
        assert "FAIL" in report

    def test_git_unavailable_no_event(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        checker = _make_checker(git_ok=False)
        outcome = run_startup(
            settings,
            checker=checker,
            control_commit=_VALID_CONTROL_COMMIT,
            output_json=False,
        )
        assert outcome.ok is False
        assert outcome.event is None
        assert outcome.manifest is None
        failed = outcome.check_result.failed_checks if outcome.check_result else []
        assert any(c.name == "git_version" for c in failed)

    def test_repo_binding_failure_no_event(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        checker = _make_checker(repo_binding_ok=False)
        outcome = run_startup(
            settings,
            checker=checker,
            control_commit=_VALID_CONTROL_COMMIT,
            output_json=False,
        )
        assert outcome.ok is False
        assert outcome.event is None

    def test_workspace_not_writable_no_event(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        checker = _make_checker(workspace_ok=False)
        outcome = run_startup(
            settings,
            checker=checker,
            control_commit=_VALID_CONTROL_COMMIT,
            output_json=False,
        )
        assert outcome.ok is False
        assert outcome.event is None
        failed_baseline = (
            outcome.check_result.failed_baseline if outcome.check_result else []
        )
        assert any(b.name == "workspace_writable" for b in failed_baseline)

    def test_docker_socket_inaccessible_no_event(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        checker = _make_checker(docker_socket_ok=False)
        outcome = run_startup(
            settings,
            checker=checker,
            control_commit=_VALID_CONTROL_COMMIT,
            output_json=False,
        )
        assert outcome.ok is False
        assert outcome.event is None

    def test_running_as_root_no_event(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        checker = _make_checker(not_root_ok=False)
        outcome = run_startup(
            settings,
            checker=checker,
            control_commit=_VALID_CONTROL_COMMIT,
            output_json=False,
        )
        assert outcome.ok is False
        assert outcome.event is None

    def test_any_failure_does_not_push(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """自检失败时 stdout 不输出注册事件 JSON。"""
        settings = _make_settings(tmp_path)
        checker = _make_checker(docker_ok=False)
        run_startup(
            settings,
            checker=checker,
            control_commit=_VALID_CONTROL_COMMIT,
            output_json=True,
        )
        captured = capsys.readouterr()
        # stdout 不应包含 event_id（注册事件未生成）。
        assert "event_id" not in captured.out


# --------------------------------------------------------------------------- #
# 验收 2：node_id 和 Git 身份稳定
# --------------------------------------------------------------------------- #


class TestNodeIdAndGitIdentityStable:
    """同一配置多次启动产生相同的 node_id 与 Git 身份。"""

    def test_same_settings_same_node_id(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        checker = _make_checker()
        outcome1 = run_startup(
            settings, checker=checker, control_commit=_VALID_CONTROL_COMMIT,
            output_json=False,
        )
        outcome2 = run_startup(
            settings, checker=checker, control_commit=_VALID_CONTROL_COMMIT,
            output_json=False,
        )
        assert outcome1.ok and outcome2.ok
        assert outcome1.node_id == outcome2.node_id == _VALID_NODE_ID
        assert (
            outcome1.event["node_id"] == outcome2.event["node_id"] == _VALID_NODE_ID
        )

    def test_git_identity_stable_across_runs(self, tmp_path: Path) -> None:
        class _StaticIdentity:
            def read_identity(self) -> dict[str, str]:
                return {"name": "runner-bot", "email": "runner@example.com"}

        settings = _make_settings(tmp_path)
        checker = _make_checker()
        provider = _StaticIdentity()
        outcome1 = run_startup(
            settings,
            checker=checker,
            git_identity_provider=provider,
            control_commit=_VALID_CONTROL_COMMIT,
            output_json=False,
        )
        outcome2 = run_startup(
            settings,
            checker=checker,
            git_identity_provider=provider,
            control_commit=_VALID_CONTROL_COMMIT,
            output_json=False,
        )
        assert outcome1.ok and outcome2.ok
        id1 = outcome1.manifest["git_identity"]
        id2 = outcome2.manifest["git_identity"]
        assert id1 == id2 == {"name": "runner-bot", "email": "runner@example.com"}

    def test_first_call_is_node_registered(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        checker = _make_checker()
        outcome = run_startup(
            settings, checker=checker, control_commit=_VALID_CONTROL_COMMIT,
            output_json=False,
        )
        assert outcome.ok
        assert outcome.event["event_type"] == "NODE_REGISTERED"


# --------------------------------------------------------------------------- #
# 验收 3：能力清单只来自可信本地配置和实际探测
# --------------------------------------------------------------------------- #


class TestCapabilitiesFromLocalConfig:
    """``NodeManifest`` 字段来自本地可信配置，不被远程任务修改。"""

    def test_capabilities_equal_labels(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path, labels=["python", "docker"])
        outcome = run_startup(
            settings, checker=_make_checker(),
            control_commit=_VALID_CONTROL_COMMIT, output_json=False,
        )
        assert outcome.ok
        assert outcome.manifest["capabilities"] == ["python", "docker"]

    def test_capacity_equal_max_concurrency(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path, max_concurrency=4)
        outcome = run_startup(
            settings, checker=_make_checker(),
            control_commit=_VALID_CONTROL_COMMIT, output_json=False,
        )
        assert outcome.ok
        assert outcome.manifest["capacity"] == 4

    def test_docker_profiles_from_config(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path, docker_profiles=["python-slim"])
        outcome = run_startup(
            settings, checker=_make_checker(),
            control_commit=_VALID_CONTROL_COMMIT, output_json=False,
        )
        assert outcome.ok
        assert outcome.manifest["docker_profiles"] == ["python-slim"]

    def test_model_aliases_from_config(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path, model_aliases=["gpt-4o", "glm-4"])
        outcome = run_startup(
            settings, checker=_make_checker(),
            control_commit=_VALID_CONTROL_COMMIT, output_json=False,
        )
        assert outcome.ok
        assert outcome.manifest["model_aliases"] == ["gpt-4o", "glm-4"]

    def test_software_version_from_config(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path, software_version="maf-runner-1.2.3")
        outcome = run_startup(
            settings, checker=_make_checker(),
            control_commit=_VALID_CONTROL_COMMIT, output_json=False,
        )
        assert outcome.ok
        assert outcome.manifest["software_version"] == "maf-runner-1.2.3"

    def test_environment_in_payload_from_probe(self, tmp_path: Path) -> None:
        """``payload.environment`` 来自 ``EnvironmentInfoProvider`` 实际探测。"""
        from maf_runner.registry import EnvironmentInfoProvider

        class _StaticEnv:
            def collect(self) -> dict[str, Any]:
                return {
                    "hostname": "test-host",
                    "os_info": {"system": "Linux"},
                    "python_version": "3.12.0",
                    "docker_version": "Docker version 24.0.0",
                    "git_version": "git version 2.45.0",
                    "cpu_count": 8,
                    "memory_mb": 16384,
                    "gpu_info": None,
                    "disk_free_mb": 51200,
                    "supported_docker_profiles": ["generic"],
                    "started_at": "2026-07-17T00:00:00Z",
                }

        settings = _make_settings(tmp_path)
        registry = RunnerRegistry(
            settings=settings,
            environment_provider=_StaticEnv(),
        )
        manifest = registry.build_manifest()
        event = registry.build_registration_event(manifest, _VALID_CONTROL_COMMIT)
        env = event["payload"]["environment"]
        assert env["hostname"] == "test-host"
        assert env["cpu_count"] == 8
        assert env["memory_mb"] == 16384
        # manifest 顶层不包含 environment 字段（schema 约束）。
        assert "environment" not in manifest
        assert "hostname" not in manifest


# --------------------------------------------------------------------------- #
# 验收：manifest 与事件 Schema 合法性
# --------------------------------------------------------------------------- #


class TestManifestAndEventSchema:
    """``NodeManifest`` 通过 ``node-v1`` Schema，注册事件通过 ``event-v1`` Schema。"""

    def test_manifest_passes_node_v1_schema(
        self, loader: SchemaLoader, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        outcome = run_startup(
            settings, checker=_make_checker(),
            control_commit=_VALID_CONTROL_COMMIT, output_json=False,
        )
        assert outcome.ok
        loader.validate(_NODE_REF, outcome.manifest)

    def test_event_passes_event_v1_schema(
        self, loader: SchemaLoader, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        outcome = run_startup(
            settings, checker=_make_checker(),
            control_commit=_VALID_CONTROL_COMMIT, output_json=False,
        )
        assert outcome.ok
        loader.validate(_EVENT_REF, outcome.event)

    def test_manifest_has_required_fields(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        outcome = run_startup(
            settings, checker=_make_checker(),
            control_commit=_VALID_CONTROL_COMMIT, output_json=False,
        )
        assert outcome.ok
        for key in (
            "schema_version",
            "node_id",
            "display_name",
            "git_identity",
            "capabilities",
            "capacity",
            "status",
            "version",
        ):
            assert key in outcome.manifest, f"missing required field: {key}"

    def test_event_has_required_fields(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        outcome = run_startup(
            settings, checker=_make_checker(),
            control_commit=_VALID_CONTROL_COMMIT, output_json=False,
        )
        assert outcome.ok
        for key in (
            "schema_version",
            "event_id",
            "event_type",
            "node_id",
            "task_id",
            "assignment_id",
            "assignment_epoch",
            "based_on_control_commit",
            "occurred_at",
            "payload",
        ):
            assert key in outcome.event, f"missing required field: {key}"

    def test_event_payload_contains_manifest_and_environment(
        self, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        outcome = run_startup(
            settings, checker=_make_checker(),
            control_commit=_VALID_CONTROL_COMMIT, output_json=False,
        )
        assert outcome.ok
        payload = outcome.event["payload"]
        assert "manifest" in payload
        assert "environment" in payload
        assert payload["manifest"]["node_id"] == _VALID_NODE_ID

    def test_event_carries_control_commit(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        outcome = run_startup(
            settings, checker=_make_checker(),
            control_commit=_VALID_CONTROL_COMMIT, output_json=False,
        )
        assert outcome.ok
        assert outcome.event["based_on_control_commit"] == _VALID_CONTROL_COMMIT


# --------------------------------------------------------------------------- #
# 验收：配置加载与校验
# --------------------------------------------------------------------------- #


class TestConfigLoading:
    """``NodeSettings`` 加载与校验。"""

    def test_new_fields_have_defaults(self, tmp_path: Path) -> None:
        """``docker_socket``、``docker_binary``、``git_binary`` 有默认值。"""
        settings = _make_settings(tmp_path)
        assert settings.docker_socket == "/var/run/docker.sock"
        assert settings.docker_binary == "docker"
        assert settings.git_binary == "git"

    def test_new_fields_from_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``MAF_DOCKER_SOCKET``、``MAF_DOCKER_BINARY``、``MAF_GIT_BINARY`` 可注入。"""
        monkeypatch.setenv("MAF_DOCKER_SOCKET", "/custom/docker.sock")
        monkeypatch.setenv("MAF_DOCKER_BINARY", "/usr/local/bin/docker")
        monkeypatch.setenv("MAF_GIT_BINARY", "/usr/local/bin/git")
        settings = NodeSettings(
            node_id=_VALID_NODE_ID,
            control_remote_url="origin",
            workspace_root=tmp_path,
            model_mapping_path=tmp_path / "model-mapping.yaml",
            capability_token_cache_path=Path("capability-tokens.db"),
            _env_file=None,
        )
        assert settings.docker_socket == "/custom/docker.sock"
        assert settings.docker_binary == "/usr/local/bin/docker"
        assert settings.git_binary == "/usr/local/bin/git"

    def test_required_fields_validated(self, tmp_path: Path) -> None:
        """缺少必填字段时 ``NodeSettings`` 构造失败。"""
        with pytest.raises(Exception):
            NodeSettings(
                # 缺少 node_id、control_remote_url、workspace_root 等
                _env_file=None,
            )


# --------------------------------------------------------------------------- #
# 验收：安全基线检查
# --------------------------------------------------------------------------- #


class TestSecurityBaseline:
    """安全基线检查：工作目录权限、Docker socket 权限、非 root 运行。"""

    def test_workspace_writable_passes(self, tmp_path: Path) -> None:
        baseline = LocalSecurityBaseline()
        result = baseline.check_workspace_writable(tmp_path)
        assert result.ok
        assert result.name == "workspace_writable"

    def test_workspace_not_exist_fails(self, tmp_path: Path) -> None:
        baseline = LocalSecurityBaseline()
        result = baseline.check_workspace_writable(tmp_path / "nonexistent")
        assert not result.ok
        assert "does not exist" in result.detail

    def test_workspace_not_directory_fails(self, tmp_path: Path) -> None:
        file_path = tmp_path / "not-a-dir"
        file_path.write_text("content")
        baseline = LocalSecurityBaseline()
        result = baseline.check_workspace_writable(file_path)
        assert not result.ok
        assert "not a directory" in result.detail

    def test_docker_socket_empty_fails(self) -> None:
        baseline = LocalSecurityBaseline()
        result = baseline.check_docker_socket("")
        assert not result.ok
        assert "empty" in result.detail

    def test_not_root_passes_on_non_root(self) -> None:
        """非 root 用户（或 Windows/非 POSIX）下检查通过。"""
        baseline = LocalSecurityBaseline()
        result = baseline.check_not_running_as_root()
        # 在 CI 中通常不以 root 运行；Windows 自动跳过。
        assert result.ok

    def test_baseline_in_startup_check(self, tmp_path: Path) -> None:
        """启动自检包含安全基线检查项。"""
        settings = _make_settings(tmp_path)
        checker = _make_checker()
        outcome = run_startup(
            settings, checker=checker,
            control_commit=_VALID_CONTROL_COMMIT, output_json=False,
        )
        assert outcome.ok
        assert outcome.check_result is not None
        baseline_names = {b.name for b in outcome.check_result.baseline_checks}
        assert "workspace_writable" in baseline_names
        assert "docker_socket" in baseline_names
        assert "not_root" in baseline_names


# --------------------------------------------------------------------------- #
# 验收：仓库绑定检查（真实 Git）
# --------------------------------------------------------------------------- #


class TestRepoBindingWithRealGit:
    """使用真实 git 验证仓库绑定检查。"""

    def test_repo_binding_passes_for_valid_repo(
        self, tmp_path: Path
    ) -> None:
        """``workspace_root`` 是合法 git 仓库时检查通过。"""
        import subprocess

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        subprocess.run(
            ["git", "init", "-q", str(repo_dir)], check=True, env=env
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "config", "user.email", "t@e.com"],
            check=True, env=env,
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "config", "user.name", "T"],
            check=True, env=env,
        )
        (repo_dir / "README.md").write_text("# test\n")
        subprocess.run(
            ["git", "-C", str(repo_dir), "add", "README.md"],
            check=True, env=env,
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "commit", "-q", "-m", "init"],
            check=True, env=env,
        )
        probe = LocalDependencyProbe()
        result = probe.check_repo_binding(repo_dir, "")
        assert result.ok
        assert result.name == "repo_binding"

    def test_repo_binding_fails_for_non_repo(self, tmp_path: Path) -> None:
        """``workspace_root`` 不是 git 仓库时检查失败。"""
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        probe = LocalDependencyProbe()
        result = probe.check_repo_binding(non_repo, "")
        assert not result.ok
        assert "not a git repository" in result.detail or "not inside" in result.detail

    def test_repo_binding_fails_for_nonexistent_path(self, tmp_path: Path) -> None:
        """``workspace_root`` 不存在时检查失败。"""
        probe = LocalDependencyProbe()
        result = probe.check_repo_binding(
            tmp_path / "nonexistent", ""
        )
        assert not result.ok
        assert "does not exist" in result.detail


# --------------------------------------------------------------------------- #
# 验收：run_startup 输出 JSON
# --------------------------------------------------------------------------- #


class TestStartupOutput:
    """``run_startup`` 在自检通过时输出注册事件 JSON 到 stdout。"""

    def test_json_output_to_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """自检通过时返回合法注册事件（不依赖 stdout 捕获，避免 structlog 干扰）。"""
        settings = _make_settings(tmp_path)
        outcome = run_startup(
            settings, checker=_make_checker(),
            control_commit=_VALID_CONTROL_COMMIT, output_json=False,
        )
        assert outcome.ok
        assert outcome.event is not None
        assert outcome.event["event_type"] == "NODE_REGISTERED"
        assert outcome.event["node_id"] == _VALID_NODE_ID
        assert outcome.event["based_on_control_commit"] == _VALID_CONTROL_COMMIT
        assert "manifest" in outcome.event["payload"]
        assert "environment" in outcome.event["payload"]

    def test_no_event_on_failure(
        self, tmp_path: Path
    ) -> None:
        """自检失败时不生成注册事件。"""
        settings = _make_settings(tmp_path)
        outcome = run_startup(
            settings, checker=_make_checker(docker_ok=False),
            control_commit=_VALID_CONTROL_COMMIT, output_json=False,
        )
        assert not outcome.ok
        assert outcome.event is None
        assert outcome.manifest is None
