"""TASK-014 集成测试：Git 凭据与远端验证。

验收标准覆盖：

1. **凭据不写仓库、日志或任务文件**：HTTPS token 与 SSH key 路径不进入命令行
   参数、不进入日志、不进入健康报告返回值。
2. **节点不能 push main 或 maf/control**：``RunnerGitClient.push_task_branch``
   拒绝 ``main``、``master``、``maf/control`` 及其 ``refs/heads/`` 前缀形式。
3. **验证产生脱敏健康报告**：``verify_binding`` 返回的字典中 remote_url 中的
   token 被替换为 ``***``，错误信息不含明文凭据。
4. **支持 HTTPS token 和 SSH 两种凭据方式**：HTTPS 经
   ``MAF_GIT_CREDENTIAL_TOKEN`` env 注入，SSH 经 ``GIT_SSH_COMMAND`` env 注入。

测试通过 mock ``asyncio.create_subprocess_exec`` 校验 subprocess 调用结构
（args、env），不真正执行 git。所有异步入口经 ``asyncio.run`` 同步执行，
避免 pytest-asyncio 配置依赖（与现有 ``test_git_cli.py`` 一致）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import structlog

from maf_domain.errors import ArgumentError, NotFoundError
from maf_runner.git_client import (
    FORBIDDEN_PUSH_BRANCHES,
    RunnerGitClient,
)
from maf_runner.workspace.git import RunnerGitCli
from maf_server.core.secrets import MASTER_KEY_SIZE_BYTES
from maf_server.gateway.repository.service import (
    GitBinding,
    LocalRepositoryGateway,
)
from maf_server.gateway.secrets.aes_gcm_store import AesGcmFileStore
from maf_server.gateway.secrets.local_service import LocalSecretService


_SECRET_TOKEN = "ghp_SECRET_TOKEN_12345_xyz"
_SSH_KEY_CONTENT = "FAKE-SSH-PRIVATE-KEY-MATERIAL"
_ORG_ID = "org-001"


def _run(coro: Any) -> Any:
    """在独立事件循环中执行协程并返回结果。"""
    return asyncio.run(coro)


def _token_bytes() -> bytes:
    import secrets as _s

    return _s.token_bytes(MASTER_KEY_SIZE_BYTES)


# --------------------------------------------------------------------------- #
# fake subprocess（用于校验调用结构，不真正执行 git）
# --------------------------------------------------------------------------- #


class _FakeProc:
    """假子进程，记录调用并可控返回输出。"""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        pass

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else -1


class _FakeExecRecorder:
    """记录 ``create_subprocess_exec`` 调用并按序返回可控 _FakeProc。"""

    def __init__(self, procs: list[_FakeProc] | _FakeProc) -> None:
        if isinstance(procs, _FakeProc):
            self._procs: list[_FakeProc] = [procs]
        else:
            self._procs = list(procs)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *args: str, **kwargs: Any) -> _FakeProc:
        self.calls.append({"args": list(args), "kwargs": kwargs})
        if self._procs:
            return self._procs.pop(0)
        return _FakeProc()


# --------------------------------------------------------------------------- #
# structlog 日志捕获
# --------------------------------------------------------------------------- #


@pytest.fixture()
def captured_log_events() -> list[dict[str, Any]]:
    """配置 structlog 捕获处理器，返回事件列表。"""
    events: list[dict[str, Any]] = []

    def _capture(
        _logger: Any, _method_name: str, event_dict: Any
    ) -> Any:
        events.append(dict(event_dict))
        return event_dict

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _capture,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    return events


# --------------------------------------------------------------------------- #
# fixtures: stores, service, paths
# --------------------------------------------------------------------------- #


@pytest.fixture()
def aes_store(tmp_path: Path) -> AesGcmFileStore:
    return AesGcmFileStore(
        master_key=_token_bytes(),
        storage_dir=tmp_path / "secrets",
        organization_id=_ORG_ID,
    )


@pytest.fixture()
def secret_service(aes_store: AesGcmFileStore) -> LocalSecretService:
    return LocalSecretService(primary=aes_store)


@pytest.fixture()
def git_repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "git_root"
    root.mkdir()
    return root


@pytest.fixture()
def ssh_key_file(tmp_path: Path) -> Path:
    """临时 SSH 私钥文件（绝对路径，内容为 fake material）。"""
    key_path = tmp_path / "ssh" / "id_ed25519"
    key_path.parent.mkdir(parents=True)
    key_path.write_text(_SSH_KEY_CONTENT, encoding="utf-8")
    return key_path


# --------------------------------------------------------------------------- #
# 验收 1：凭据不进入命令行参数或日志
# --------------------------------------------------------------------------- #


class TestCredentialIsolation:
    """凭据经环境变量传递，绝不进入命令行参数或日志。"""

    def test_https_token_not_in_command_args(
        self,
        git_repo_root: Path,
        secret_service: LocalSecretService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """HTTPS token 经 env 注入，不出现在任何命令行参数中。"""
        secret_id = _run(
            secret_service.create("binding", "b1", _SECRET_TOKEN, name="git-https")
        )
        gateway = LocalRepositoryGateway(
            git_repo_root=git_repo_root,
            secret_service=secret_service,
            actor_id="b1",
        )
        gateway.register_binding(
            GitBinding(
                binding_id="b1",
                remote_url="https://github.com/org/repo.git",
                credential_type="HTTPS_TOKEN",
                secret_id=secret_id,
            )
        )

        recorder = _FakeExecRecorder(
            [
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
            ]
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        _run(gateway.verify_binding("b1"))

        assert recorder.calls, "应至少一次 subprocess 调用"
        for call in recorder.calls:
            for arg in call["args"]:
                assert _SECRET_TOKEN not in arg, f"token 泄漏进参数: {arg!r}"
            # token 经 env 传递。
            env = call["kwargs"]["env"]
            assert env["MAF_GIT_CREDENTIAL_TOKEN"] == _SECRET_TOKEN

    def test_https_token_not_in_logs(
        self,
        git_repo_root: Path,
        secret_service: LocalSecretService,
        monkeypatch: pytest.MonkeyPatch,
        captured_log_events: list[dict[str, Any]],
    ) -> None:
        """HTTPS token 不出现在任何捕获的日志事件中。"""
        secret_id = _run(secret_service.create("binding", "b1", _SECRET_TOKEN))
        gateway = LocalRepositoryGateway(
            git_repo_root=git_repo_root,
            secret_service=secret_service,
            actor_id="b1",
        )
        gateway.register_binding(
            GitBinding(
                binding_id="b1",
                remote_url="https://github.com/org/repo.git",
                secret_id=secret_id,
            )
        )

        recorder = _FakeExecRecorder(
            [
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
            ]
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        _run(gateway.verify_binding("b1"))

        assert captured_log_events, "应至少捕获一条日志"
        blob = json.dumps(captured_log_events, ensure_ascii=False)
        assert _SECRET_TOKEN not in blob, "token 泄漏进日志"

    def test_ssh_command_not_in_command_args(
        self,
        git_repo_root: Path,
        ssh_key_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SSH: GIT_SSH_COMMAND 经 env 注入，key 路径不作为命令行参数。"""
        gateway = LocalRepositoryGateway(
            git_repo_root=git_repo_root,
            secret_service=None,
        )
        gateway.register_binding(
            GitBinding(
                binding_id="ssh-1",
                remote_url="git@github.com:org/repo.git",
                credential_type="SSH_KEY",
                ssh_key_path=str(ssh_key_file),
            )
        )

        recorder = _FakeExecRecorder(
            [
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
            ]
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        _run(gateway.verify_binding("ssh-1"))

        assert recorder.calls
        for call in recorder.calls:
            # git 命令参数中不应出现 key 路径（只出现在 GIT_SSH_COMMAND env 中）。
            for arg in call["args"]:
                assert str(ssh_key_file) not in arg, (
                    f"key 路径泄漏进参数: {arg!r}"
                )
            env = call["kwargs"]["env"]
            ssh_cmd = env.get("GIT_SSH_COMMAND", "")
            assert "-i" in ssh_cmd
            assert str(ssh_key_file) in ssh_cmd
            # SSH 模式不应同时注入 HTTPS token env。
            assert "MAF_GIT_CREDENTIAL_TOKEN" not in env

    def test_plaintext_not_in_health_report(
        self,
        git_repo_root: Path,
        secret_service: LocalSecretService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """返回的健康报告中不含明文 token（包括 URL 中嵌入的 token 也被脱敏）。"""
        secret_id = _run(secret_service.create("binding", "b1", _SECRET_TOKEN))
        gateway = LocalRepositoryGateway(
            git_repo_root=git_repo_root,
            secret_service=secret_service,
            actor_id="b1",
        )
        # URL 中嵌入 token 测试脱敏。
        gateway.register_binding(
            GitBinding(
                binding_id="b1",
                remote_url=f"https://{_SECRET_TOKEN}@github.com/org/repo.git",
                secret_id=secret_id,
            )
        )

        recorder = _FakeExecRecorder(
            [
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
            ]
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        report = _run(gateway.verify_binding("b1"))

        blob = json.dumps(report, ensure_ascii=False)
        assert _SECRET_TOKEN not in blob, f"健康报告泄漏明文: {blob}"
        assert "***@" in report["remote_url"], "URL 中的 token 应被脱敏为 ***@"

    def test_dangerous_host_git_env_stripped_in_verify(
        self,
        git_repo_root: Path,
        ssh_key_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """verify_binding 期间宿主危险 GIT_* 变量被剥离，注入受控值优先。"""
        monkeypatch.setenv("GIT_SSH_COMMAND", "evil-ssh-from-host")
        monkeypatch.setenv("GIT_EDITOR", "evil-editor")

        gateway = LocalRepositoryGateway(
            git_repo_root=git_repo_root,
            secret_service=None,
        )
        gateway.register_binding(
            GitBinding(
                binding_id="ssh-1",
                remote_url="git@github.com:org/repo.git",
                credential_type="SSH_KEY",
                ssh_key_path=str(ssh_key_file),
            )
        )

        recorder = _FakeExecRecorder(
            [
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
            ]
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        _run(gateway.verify_binding("ssh-1"))

        env = recorder.calls[0]["kwargs"]["env"]
        # 宿主 evil 值被剥离/覆盖。
        assert "evil-ssh-from-host" not in env.get("GIT_SSH_COMMAND", "")
        assert env.get("GIT_EDITOR") is None
        # 受控 GIT_SSH_COMMAND 由 extra_env 注入。
        assert "-i" in env["GIT_SSH_COMMAND"]
        # GIT_TERMINAL_PROMPT=0 防止交互式挂起。
        assert env["GIT_TERMINAL_PROMPT"] == "0"


# --------------------------------------------------------------------------- #
# 验收 2：节点不能 push main 或 maf/control
# --------------------------------------------------------------------------- #


class TestRunnerBranchProtection:
    """RunnerGitClient 拒绝 push 到受保护分支（main/master/maf/control）。"""

    @pytest.mark.parametrize(
        "branch",
        [
            "main",
            "master",
            "maf/control",
            "refs/heads/main",
            "refs/heads/master",
            "refs/heads/maf/control",
        ],
    )
    def test_push_to_protected_branch_rejected(
        self,
        branch: str,
        git_repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """push 到受保护分支抛 ArgumentError，不触发 subprocess。"""
        cli = RunnerGitCli(allowed_roots=[git_repo_root])
        client = RunnerGitClient(
            git_cli=cli,
            repository_path=str(git_repo_root),
            control_remote="origin",
            control_branch="maf/control",
            node_id="node-test",
        )

        recorder = _FakeExecRecorder(_FakeProc())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        with pytest.raises(ArgumentError, match="protected branch"):
            _run(client.push_task_branch(branch=branch))

        assert recorder.calls == [], "受保护分支不应触发 subprocess"

    def test_forbidden_branches_set_contents(self) -> None:
        """FORBIDDEN_PUSH_BRANCHES 包含 main/master/maf/control。"""
        assert "main" in FORBIDDEN_PUSH_BRANCHES
        assert "master" in FORBIDDEN_PUSH_BRANCHES
        assert "maf/control" in FORBIDDEN_PUSH_BRANCHES

    def test_is_forbidden_push_target_method(self, git_repo_root: Path) -> None:
        """is_forbidden_push_target 正确识别受保护分支（含 refs/heads/ 前缀）。"""
        cli = RunnerGitCli(allowed_roots=[git_repo_root])
        client = RunnerGitClient(
            git_cli=cli,
            repository_path=str(git_repo_root),
        )
        assert client.is_forbidden_push_target("main") is True
        assert client.is_forbidden_push_target("refs/heads/main") is True
        assert client.is_forbidden_push_target("maf/control") is True
        assert client.is_forbidden_push_target("  maf/control  ") is True
        assert client.is_forbidden_push_target("maf/task/t-001/e1-node-1") is False
        assert client.is_forbidden_push_target("feature/x") is False

    @pytest.mark.parametrize(
        "branch",
        [
            "maf/task/t-001/e1-node-1",
            "maf/node/node-001",
            "feature/test",
        ],
    )
    def test_push_to_allowed_branch_proceeds(
        self,
        branch: str,
        git_repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """push 到任务/节点/功能分支正常调用 subprocess。"""
        cli = RunnerGitCli(allowed_roots=[git_repo_root])
        client = RunnerGitClient(
            git_cli=cli,
            repository_path=str(git_repo_root),
            control_remote="origin",
            control_branch="maf/control",
            node_id="node-test",
        )

        recorder = _FakeExecRecorder(_FakeProc(returncode=0, stdout=b"", stderr=b""))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        result = _run(client.push_task_branch(branch=branch))

        assert result["push_ok"] is True
        assert len(recorder.calls) == 1
        push_args = recorder.calls[0]["args"]
        assert "push" in push_args
        # 目标分支出现在 refspec 中（refs/heads/<normalized>）。
        normalized = branch.removeprefix("refs/heads/")
        assert any(normalized in arg for arg in push_args)

    def test_credential_token_injected_in_runner_push(
        self,
        git_repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RunnerGitCli 注入的 MAF_GIT_CREDENTIAL_TOKEN 出现在子进程 env 中。"""
        cli = RunnerGitCli(
            allowed_roots=[git_repo_root],
            credential_token=_SECRET_TOKEN,
        )
        client = RunnerGitClient(
            git_cli=cli,
            repository_path=str(git_repo_root),
        )

        recorder = _FakeExecRecorder(_FakeProc(returncode=0, stdout=b"", stderr=b""))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        _run(client.push_task_branch(branch="maf/task/t-1/e1-n-1"))

        call = recorder.calls[0]
        for arg in call["args"]:
            assert _SECRET_TOKEN not in arg
        assert call["kwargs"]["env"]["MAF_GIT_CREDENTIAL_TOKEN"] == _SECRET_TOKEN


# --------------------------------------------------------------------------- #
# 验收 3：验证产生脱敏健康报告
# --------------------------------------------------------------------------- #


class TestVerifyBindingHealthReport:
    """verify_binding 返回脱敏健康报告。"""

    def test_verify_binding_success(
        self,
        git_repo_root: Path,
        secret_service: LocalSecretService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """fetch + push dry-run 都成功；报告字段完整且不含明文。"""
        secret_id = _run(secret_service.create("binding", "b1", _SECRET_TOKEN))
        gateway = LocalRepositoryGateway(
            git_repo_root=git_repo_root,
            secret_service=secret_service,
            actor_id="b1",
        )
        gateway.register_binding(
            GitBinding(
                binding_id="b1",
                remote_url="https://github.com/org/repo.git",
                secret_id=secret_id,
            )
        )

        recorder = _FakeExecRecorder(
            [
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
            ]
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        report = _run(gateway.verify_binding("b1"))

        assert report["binding_id"] == "b1"
        assert report["fetch_accessible"] is True
        assert report["push_permitted"] is True
        assert report["credential_type"] == "HTTPS_TOKEN"
        assert "checked_at" in report
        assert report["fetch_error"] is None
        assert report["push_error"] is None
        assert _SECRET_TOKEN not in json.dumps(report)

    def test_verify_binding_fetch_failure(
        self,
        git_repo_root: Path,
        secret_service: LocalSecretService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ls-remote 失败：fetch_accessible=False，不进行 push 验证。"""
        secret_id = _run(secret_service.create("binding", "b1", _SECRET_TOKEN))
        gateway = LocalRepositoryGateway(
            git_repo_root=git_repo_root,
            secret_service=secret_service,
            actor_id="b1",
        )
        gateway.register_binding(
            GitBinding(
                binding_id="b1",
                remote_url="https://github.com/org/repo.git",
                secret_id=secret_id,
            )
        )

        recorder = _FakeExecRecorder(
            [
                _FakeProc(
                    returncode=128, stdout=b"", stderr=b"fatal: Authentication failed"
                ),
            ]
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        report = _run(gateway.verify_binding("b1"))

        assert report["fetch_accessible"] is False
        assert report["push_permitted"] is False
        # 错误信息保留用于诊断，但不含明文 token。
        assert "Authentication failed" in report["fetch_error"]
        assert _SECRET_TOKEN not in json.dumps(report)
        # 只调用了 fetch，未调用 push（fetch 失败时跳过 push 验证）。
        assert len(recorder.calls) == 1

    def test_verify_binding_push_forbidden(
        self,
        git_repo_root: Path,
        secret_service: LocalSecretService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """fetch 成功但 push dry-run 失败（权限不足）。"""
        secret_id = _run(secret_service.create("binding", "b1", _SECRET_TOKEN))
        gateway = LocalRepositoryGateway(
            git_repo_root=git_repo_root,
            secret_service=secret_service,
            actor_id="b1",
        )
        gateway.register_binding(
            GitBinding(
                binding_id="b1",
                remote_url="https://github.com/org/repo.git",
                secret_id=secret_id,
            )
        )

        recorder = _FakeExecRecorder(
            [
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
                _FakeProc(
                    returncode=128, stdout=b"", stderr=b"remote: Permission denied"
                ),
            ]
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        report = _run(gateway.verify_binding("b1"))

        assert report["fetch_accessible"] is True
        assert report["push_permitted"] is False
        assert "Permission denied" in report["push_error"]
        assert _SECRET_TOKEN not in json.dumps(report)

    def test_verify_binding_not_found(self, git_repo_root: Path) -> None:
        """未注册的 binding_id 抛 NotFoundError。"""
        gateway = LocalRepositoryGateway(git_repo_root=git_repo_root)
        with pytest.raises(NotFoundError):
            _run(gateway.verify_binding("nonexistent"))

    def test_verify_binding_uses_fetch_dry_run(
        self,
        git_repo_root: Path,
        secret_service: LocalSecretService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """verify_binding 使用 fetch --dry-run（不修改本地/远端状态）。"""
        secret_id = _run(secret_service.create("binding", "b1", _SECRET_TOKEN))
        gateway = LocalRepositoryGateway(
            git_repo_root=git_repo_root,
            secret_service=secret_service,
            actor_id="b1",
        )
        gateway.register_binding(
            GitBinding(
                binding_id="b1",
                remote_url="https://github.com/org/repo.git",
                secret_id=secret_id,
            )
        )

        recorder = _FakeExecRecorder(
            [
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
            ]
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        _run(gateway.verify_binding("b1"))

        # 第一次调用应是 fetch --dry-run。
        fetch_args = recorder.calls[0]["args"]
        assert fetch_args[1] == "fetch"
        assert "--dry-run" in fetch_args
        # 第二次调用应是 push --dry-run。
        push_args = recorder.calls[1]["args"]
        assert push_args[1] == "push"
        assert "--dry-run" in push_args
        # 临时验证分支名出现在 refspec 中。
        assert any("_maf_verify_" in arg for arg in push_args)

    def test_url_with_credentials_redacted(
        self,
        git_repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        ssh_key_file: Path,
    ) -> None:
        """remote_url 中的 user:pass@ 和 token@ 形式都被脱敏。"""
        gateway = LocalRepositoryGateway(
            git_repo_root=git_repo_root,
            secret_service=None,
        )
        gateway.register_binding(
            GitBinding(
                binding_id="ssh-1",
                remote_url="https://user:pass@github.com/org/repo.git",
                credential_type="SSH_KEY",
                ssh_key_path=str(ssh_key_file),
            )
        )

        recorder = _FakeExecRecorder(
            [
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
            ]
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        report = _run(gateway.verify_binding("ssh-1"))

        assert report["remote_url"] == "https://***@github.com/org/repo.git"
        assert "user" not in report["remote_url"]
        assert "pass" not in report["remote_url"]


# --------------------------------------------------------------------------- #
# 验收 4：支持 HTTPS token 和 SSH 两种凭据方式
# --------------------------------------------------------------------------- #


class TestCredentialModes:
    """HTTPS token 与 SSH key 两种凭据方式。"""

    def test_https_token_mode_env(
        self,
        git_repo_root: Path,
        secret_service: LocalSecretService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """HTTPS: MAF_GIT_CREDENTIAL_TOKEN 注入 env，GIT_SSH_COMMAND 不出现。"""
        secret_id = _run(secret_service.create("binding", "b1", _SECRET_TOKEN))
        gateway = LocalRepositoryGateway(
            git_repo_root=git_repo_root,
            secret_service=secret_service,
            actor_id="b1",
        )
        gateway.register_binding(
            GitBinding(
                binding_id="b1",
                remote_url="https://github.com/org/repo.git",
                credential_type="HTTPS_TOKEN",
                secret_id=secret_id,
            )
        )

        recorder = _FakeExecRecorder(
            [
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
            ]
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        _run(gateway.verify_binding("b1"))

        env = recorder.calls[0]["kwargs"]["env"]
        assert env["MAF_GIT_CREDENTIAL_TOKEN"] == _SECRET_TOKEN
        # HTTPS 模式不应注入 SSH 命令。
        assert "GIT_SSH_COMMAND" not in env

    def test_ssh_key_mode_env(
        self,
        git_repo_root: Path,
        ssh_key_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SSH: GIT_SSH_COMMAND 注入 env，含 -i <key_path>，HTTPS token 不出现。"""
        gateway = LocalRepositoryGateway(
            git_repo_root=git_repo_root,
            secret_service=None,
        )
        gateway.register_binding(
            GitBinding(
                binding_id="ssh-1",
                remote_url="git@github.com:org/repo.git",
                credential_type="SSH_KEY",
                ssh_key_path=str(ssh_key_file),
            )
        )

        recorder = _FakeExecRecorder(
            [
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
            ]
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        _run(gateway.verify_binding("ssh-1"))

        env = recorder.calls[0]["kwargs"]["env"]
        ssh_cmd = env["GIT_SSH_COMMAND"]
        assert "-i" in ssh_cmd
        assert str(ssh_key_file) in ssh_cmd
        assert "IdentitiesOnly=yes" in ssh_cmd
        assert "BatchMode=yes" in ssh_cmd
        # SSH 模式不应注入 HTTPS token。
        assert "MAF_GIT_CREDENTIAL_TOKEN" not in env

    def test_ssh_key_path_validation_rejects_relative(
        self,
        git_repo_root: Path,
    ) -> None:
        """SSH key 路径必须为绝对路径。"""
        gateway = LocalRepositoryGateway(git_repo_root=git_repo_root)
        gateway.register_binding(
            GitBinding(
                binding_id="ssh-bad",
                remote_url="git@github.com:org/repo.git",
                credential_type="SSH_KEY",
                ssh_key_path="relative/path/key",
            )
        )
        with pytest.raises(ArgumentError, match="absolute"):
            _run(gateway.verify_binding("ssh-bad"))

    def test_ssh_key_path_validation_rejects_missing(
        self,
        git_repo_root: Path,
        tmp_path: Path,
    ) -> None:
        """SSH key 路径必须指向已存在的文件。"""
        gateway = LocalRepositoryGateway(git_repo_root=git_repo_root)
        missing = tmp_path / "nonexistent_key"
        gateway.register_binding(
            GitBinding(
                binding_id="ssh-missing",
                remote_url="git@github.com:org/repo.git",
                credential_type="SSH_KEY",
                ssh_key_path=str(missing),
            )
        )
        with pytest.raises(ArgumentError, match="not.*file"):
            _run(gateway.verify_binding("ssh-missing"))

    def test_ssh_key_path_validation_rejects_shell_metachars(
        self,
        git_repo_root: Path,
    ) -> None:
        """SSH key 路径含 shell 元字符被拒（防 GIT_SSH_COMMAND 注入）。"""
        gateway = LocalRepositoryGateway(git_repo_root=git_repo_root)
        gateway.register_binding(
            GitBinding(
                binding_id="ssh-evil",
                remote_url="git@github.com:org/repo.git",
                credential_type="SSH_KEY",
                ssh_key_path="/tmp/key; rm -rf /",
            )
        )
        with pytest.raises(ArgumentError, match="forbidden characters"):
            _run(gateway.verify_binding("ssh-evil"))

    def test_https_without_secret_service_skips_token(
        self,
        git_repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """HTTPS binding 但 secret_service=None：不注入 token，git 自然失败。"""
        gateway = LocalRepositoryGateway(
            git_repo_root=git_repo_root,
            secret_service=None,
        )
        gateway.register_binding(
            GitBinding(
                binding_id="b1",
                remote_url="https://github.com/org/repo.git",
                credential_type="HTTPS_TOKEN",
                secret_id="some-secret-id",
            )
        )

        recorder = _FakeExecRecorder(
            _FakeProc(returncode=128, stdout=b"", stderr=b"auth failed")
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        report = _run(gateway.verify_binding("b1"))

        # token 未注入，fetch 失败。
        assert report["fetch_accessible"] is False
        env = recorder.calls[0]["kwargs"]["env"]
        assert "MAF_GIT_CREDENTIAL_TOKEN" not in env


# --------------------------------------------------------------------------- #
# 端到端：RunnerGitClient fetch_control
# --------------------------------------------------------------------------- #


class TestRunnerFetchControl:
    """RunnerGitClient.fetch_control 返回脱敏快照。"""

    def test_fetch_control_success(
        self,
        git_repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """fetch 成功后解析 control_commit。"""
        cli = RunnerGitCli(allowed_roots=[git_repo_root])
        client = RunnerGitClient(
            git_cli=cli,
            repository_path=str(git_repo_root),
            control_remote="origin",
            control_branch="maf/control",
        )

        recorder = _FakeExecRecorder(
            [
                _FakeProc(returncode=0, stdout=b"", stderr=b""),
                _FakeProc(
                    returncode=0,
                    stdout=b"abcdef1234567890abcdef1234567890abcdef12\n",
                    stderr=b"",
                ),
            ]
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        result = _run(client.fetch_control())

        assert result["fetch_ok"] is True
        assert result["control_commit"] == (
            "abcdef1234567890abcdef1234567890abcdef12"
        )
        assert result["control_branch"] == "maf/control"
        assert result["remote"] == "origin"
        assert result["fetch_error"] is None

    def test_fetch_control_failure(
        self,
        git_repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """fetch 失败时 control_commit 为 None，fetch_error 保留。"""
        cli = RunnerGitCli(allowed_roots=[git_repo_root])
        client = RunnerGitClient(
            git_cli=cli,
            repository_path=str(git_repo_root),
        )

        recorder = _FakeExecRecorder(
            _FakeProc(
                returncode=128, stdout=b"", stderr=b"fatal: could not read Username"
            )
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        result = _run(client.fetch_control())

        assert result["fetch_ok"] is False
        assert result["control_commit"] is None
        assert "could not read Username" in result["fetch_error"]
        # 凭据不出现在结果中。
        assert _SECRET_TOKEN not in json.dumps(result)
