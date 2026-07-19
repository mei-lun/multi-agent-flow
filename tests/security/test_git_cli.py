"""TASK-012 安全测试：安全 Git CLI 封装。

验收标准覆盖：

1. 命令注入被阻止（无 ``shell=True``，参数化；``;``、``$()``、``|``、反引号
   被作为字面参数，不被 shell 解释）。
2. 仅白名单 git 子命令可执行；全局选项注入（``-c``/``-C`` 前置）被拒绝。
3. 危险标志（``--upload-pack``、``--receive-pack``、``--ext-diff`` 等）被拒绝。
4. 所有路径限制在仓库/工作区根目录；``..`` 遍历与越界路径被拒绝。
5. 凭据不进入命令行参数或日志（经环境变量传递）。
6. 命令超时、输出限长和凭据脱敏生效。

测试使用真实 git（2.45+）引导临时仓库，并通过 monkeypatch 校验 subprocess 调用
结构。所有异步入口经 ``asyncio.run`` 同步执行，避免 pytest-asyncio 配置依赖。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import structlog

from maf_domain.errors import ArgumentError, ExternalDependencyError
from maf_repository_adapters.git_cli import (
    ALLOWED_SUBCOMMANDS,
    FORBIDDEN_FLAGS,
    SubprocessGitCli,
)
from maf_repository_adapters import GitCommandError


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def allowed_root(tmp_path: Path) -> Path:
    """受控根目录，所有 repository_path 必须位于其内。"""
    root = tmp_path / "allowed"
    root.mkdir()
    return root


@pytest.fixture()
def git_repo(allowed_root: Path) -> Path:
    """在受控根目录下引导一个真实 git 仓库（含一个提交）。"""
    repo = allowed_root / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test Bot",
        "GIT_AUTHOR_EMAIL": "bot@example.test",
        "GIT_COMMITTER_NAME": "Test Bot",
        "GIT_COMMITTER_EMAIL": "bot@example.test",
    }
    subprocess.run(
        ["git", "init", "-q", str(repo)], check=True, env=env
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "bot@example.test"],
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test Bot"],
        check=True,
        env=env,
    )
    # 提交一个普通文件 + 一个敏感路径文件（用于脱敏测试）。
    (repo / "README.txt").write_text("hello\n", encoding="utf-8")
    sensitive_dir = repo / ".ssh"
    sensitive_dir.mkdir(exist_ok=True)
    (sensitive_dir / "id_rsa").write_text("FAKE-KEY-MATERIAL\n", encoding="utf-8")
    big = repo / "big.txt"
    big.write_text("A" * 4096, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "."], check=True, env=env
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "initial"],
        check=True,
        env=env,
    )
    return repo


def _make_cli(
    allowed_root: Path,
    *,
    extra_env: dict[str, str] | None = None,
    max_output_bytes: int = 1024 * 1024,
    git_binary: str = "git",
) -> SubprocessGitCli:
    return SubprocessGitCli(
        allowed_roots=[allowed_root],
        extra_env=extra_env,
        max_output_bytes=max_output_bytes,
        git_binary=git_binary,
    )


def _run(coro: Any) -> Any:
    """在独立事件循环中执行协程并返回结果。"""
    return asyncio.run(coro)


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
        block: bool = False,
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._block = block
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._block:
            # 永不完成，直到被 wait_for 取消。
            await asyncio.Event().wait()
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else -1


class _FakeExecRecorder:
    """记录 create_subprocess_exec 的参数与 env，返回 _FakeProc。"""

    def __init__(self, proc: _FakeProc) -> None:
        self._proc = proc
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *args: str, **kwargs: Any) -> _FakeProc:
        self.calls.append({"args": list(args), "kwargs": kwargs})
        return self._proc


@pytest.fixture()
def captured_log_events() -> list[dict[str, Any]]:
    """配置 structlog 捕获处理器，返回事件列表。"""
    events: list[dict[str, Any]] = []

    def _capture(
        _logger: Any, _method_name: str, event_dict: dict[str, Any]
    ) -> dict[str, Any]:
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
# 验收 1：命令注入被阻止（参数化执行，无 shell）
# --------------------------------------------------------------------------- #


class TestCommandInjectionBlocked:
    """shell 元字符被作为字面参数，不被解释执行。"""

    @pytest.mark.parametrize(
        "payload",
        [
            "--oneline; rm -rf /",
            "main; echo PWNED",
            "$(whoami)",
            "`id`",
            "main | cat",
            "main && cat /etc/passwd",
            "main > /tmp/evil",
        ],
    )
    def test_shell_metachars_passed_as_literal_single_arg(
        self,
        allowed_root: Path,
        git_repo: Path,
        payload: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """注入串必须作为单个字面参数传递给 create_subprocess_exec，不被 shell 拆分。"""
        cli = _make_cli(allowed_root)
        recorder = _FakeExecRecorder(_FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        _run(cli.run(str(git_repo), ["log", payload], 5))

        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        # 第一个 arg 是 git binary，其后为参数数组；payload 必须是单一元素。
        assert call["args"][0] == "git"
        assert call["args"][1] == "log"
        assert call["args"][2] == payload, "注入串应作为单一字面参数，未被 shell 拆分"
        assert len(call["args"]) == 3
        # 确保没有传 shell=True（create_subprocess_exec 本身不支持，但显式断言 kwargs）。
        assert call["kwargs"].get("shell") in (None, False)

    def test_no_shell_subprocess_used(
        self,
        allowed_root: Path,
        git_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """应使用 create_subprocess_exec，而非 create_subprocess_shell。"""
        cli = _make_cli(allowed_root)
        recorder = _FakeExecRecorder(_FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)
        shell_calls: list[Any] = []
        monkeypatch.setattr(
            asyncio,
            "create_subprocess_shell",
            lambda *a, **k: shell_calls.append((a, k)),
        )

        _run(cli.run(str(git_repo), ["status"], 5))

        assert len(recorder.calls) == 1
        assert shell_calls == [], "禁止使用 create_subprocess_shell"


# --------------------------------------------------------------------------- #
# 验收 2：仅白名单 git 子命令可执行
# --------------------------------------------------------------------------- #


class TestSubcommandWhitelist:
    """非白名单子命令与全局选项注入被拒绝，且不触发 subprocess。"""

    def test_non_whitelisted_subcommand_rejected(
        self,
        allowed_root: Path,
        git_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cli = _make_cli(allowed_root)
        recorder = _FakeExecRecorder(_FakeProc())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        # config 可改写 git 行为（如 core.sshCommand），必须在白名单外。
        with pytest.raises(ArgumentError, match="whitelist"):
            _run(cli.run(str(git_repo), ["config", "core.sshCommand", "evil"], 5))
        assert recorder.calls == [], "非白名单命令不应触发 subprocess"

    @pytest.mark.parametrize(
        "forbidden_subcommand",
        ["config", "remote", "filter-branch", "submodule", "bisect", "daemon"],
    )
    def test_dangerous_subcommands_excluded_from_whitelist(
        self, forbidden_subcommand: str
    ) -> None:
        assert forbidden_subcommand not in ALLOWED_SUBCOMMANDS

    def test_global_option_injection_rejected(
        self,
        allowed_root: Path,
        git_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``git -c core.sshCommand=evil fetch`` 的全局 -c 前置被阻止。"""
        cli = _make_cli(allowed_root)
        recorder = _FakeExecRecorder(_FakeProc())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        with pytest.raises(ArgumentError, match="whitelist"):
            _run(
                cli.run(
                    str(git_repo),
                    ["-c", "core.sshCommand=evil", "fetch"],
                    5,
                )
            )
        assert recorder.calls == []

    def test_global_change_dir_injection_rejected(
        self,
        allowed_root: Path,
        git_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``git -C /etc ...`` 全局换目录被阻止（arguments[0] 必须是子命令）。"""
        cli = _make_cli(allowed_root)
        recorder = _FakeExecRecorder(_FakeProc())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        with pytest.raises(ArgumentError, match="whitelist"):
            _run(cli.run(str(git_repo), ["-C", "/etc", "status"], 5))
        assert recorder.calls == []

    def test_empty_arguments_rejected(self, allowed_root: Path, git_repo: Path) -> None:
        cli = _make_cli(allowed_root)
        with pytest.raises(ArgumentError, match="empty"):
            _run(cli.run(str(git_repo), [], 5))


# --------------------------------------------------------------------------- #
# 验收 3：危险标志被拒绝
# --------------------------------------------------------------------------- #


class TestForbiddenFlags:
    """--upload-pack/--receive-pack/--ext-diff 等命令执行向量被拒绝。"""

    @pytest.mark.parametrize(
        "args",
        [
            ["fetch", "origin", "--upload-pack=evil-cmd"],
            ["fetch", "--upload-pack", "evil-cmd", "origin"],
            ["clone", "--upload-pack=evil", "url"],
            ["push", "--receive-pack=evil", "origin", "main"],
            ["push", "origin", "--receive-pack", "evil"],
            ["diff", "--ext-diff"],
            ["log", "--ext-diff"],
            ["rebase", "--exec=evil"],
            ["rebase", "--sequence-editor=evil"],
        ],
    )
    def test_forbidden_flag_rejected(
        self,
        allowed_root: Path,
        git_repo: Path,
        args: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cli = _make_cli(allowed_root)
        recorder = _FakeExecRecorder(_FakeProc())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        with pytest.raises(ArgumentError, match="forbidden git flag"):
            _run(cli.run(str(git_repo), args, 5))
        assert recorder.calls == []

    def test_forbidden_flags_set_contents(self) -> None:
        """白名单标志集合覆盖关键命令执行向量。"""
        for flag in [
            "--upload-pack",
            "--receive-pack",
            "--ext-diff",
            "--exec",
            "--sequence-editor",
        ]:
            assert flag in FORBIDDEN_FLAGS

    def test_legitimate_commit_c_flag_allowed(
        self, allowed_root: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``git commit -c <commit>``（复用消息）是合法用法，不应被误拒。

        -c/-C 作为子命令选项而非全局选项，因 arguments[0]=='commit' 已通过白名单。
        """
        cli = _make_cli(allowed_root)
        recorder = _FakeExecRecorder(_FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        _run(cli.run(str(git_repo), ["commit", "-c", "HEAD"], 5))
        assert len(recorder.calls) == 1


# --------------------------------------------------------------------------- #
# 验收 4：路径限制在仓库/工作区根目录
# --------------------------------------------------------------------------- #


class TestPathConfinement:
    """repository_path 必须位于 allowed_roots 之内。"""

    def test_path_outside_allowed_root_rejected(
        self, allowed_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _make_cli(allowed_root)
        recorder = _FakeExecRecorder(_FakeProc())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(ArgumentError, match="outside allowed roots"):
            _run(cli.run(str(outside), ["status"], 5))
        assert recorder.calls == []

    def test_dotdot_traversal_rejected(
        self, allowed_root: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _make_cli(allowed_root)
        recorder = _FakeExecRecorder(_FakeProc())
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        traversal = str(git_repo / ".." / ".." / "etc")
        with pytest.raises(ArgumentError, match="outside allowed roots"):
            _run(cli.run(traversal, ["status"], 5))
        assert recorder.calls == []

    def test_empty_repository_path_rejected(self, allowed_root: Path) -> None:
        cli = _make_cli(allowed_root)
        with pytest.raises(ArgumentError, match="empty"):
            _run(cli.run("", ["status"], 5))

    def test_subpath_within_root_allowed(
        self, allowed_root: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """受控根目录下的子路径允许执行。"""
        cli = _make_cli(allowed_root)
        recorder = _FakeExecRecorder(_FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        _run(cli.run(str(git_repo), ["status"], 5))
        assert len(recorder.calls) == 1
        assert recorder.calls[0]["kwargs"]["cwd"] == str(git_repo.resolve())


# --------------------------------------------------------------------------- #
# 验收 5：凭据不进入命令行参数或日志
# --------------------------------------------------------------------------- #


_SECRET_TOKEN = "ghp_SECRET_TOKEN_12345_xyz"


class TestCredentialIsolation:
    """凭据经环境变量传递，绝不进入命令行参数或日志。"""

    def test_credentials_not_in_command_args(
        self,
        allowed_root: Path,
        git_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cli = _make_cli(
            allowed_root,
            extra_env={"MAF_GIT_CREDENTIAL_TOKEN": _SECRET_TOKEN},
        )
        recorder = _FakeExecRecorder(_FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        _run(cli.run(str(git_repo), ["push", "origin", "main"], 5))

        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        # 凭据绝不出现在任何命令行参数中。
        for arg in call["args"]:
            assert _SECRET_TOKEN not in arg, f"凭据泄漏进参数: {arg!r}"
        # 凭据经环境变量传递。
        env = call["kwargs"]["env"]
        assert env["MAF_GIT_CREDENTIAL_TOKEN"] == _SECRET_TOKEN

    def test_credentials_not_in_logs(
        self,
        allowed_root: Path,
        git_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        captured_log_events: list[dict[str, Any]],
    ) -> None:
        cli = _make_cli(
            allowed_root,
            extra_env={"MAF_GIT_CREDENTIAL_TOKEN": _SECRET_TOKEN, "password": "s3cr3t"},
        )
        recorder = _FakeExecRecorder(_FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        _run(cli.run(str(git_repo), ["status"], 5))

        assert captured_log_events, "应至少捕获一条日志"
        blob = json.dumps(captured_log_events, ensure_ascii=False)
        assert _SECRET_TOKEN not in blob, "凭据泄漏进日志"
        assert "s3cr3t" not in blob, "敏感 env 值泄漏进日志"

    def test_dangerous_host_git_env_stripped(
        self, allowed_root: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """宿主环境中可注入命令的 GIT_* 变量在合并前被剥离。"""
        monkeypatch.setenv("GIT_SSH_COMMAND", "evil-ssh")
        monkeypatch.setenv("GIT_EDITOR", "evil-editor")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.sshCommand")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "evil")
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")

        cli = _make_cli(allowed_root)
        recorder = _FakeExecRecorder(_FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        _run(cli.run(str(git_repo), ["status"], 5))

        env = recorder.calls[0]["kwargs"]["env"]
        assert env.get("GIT_SSH_COMMAND") is None
        assert env.get("GIT_EDITOR") is None
        assert "GIT_CONFIG_KEY_0" not in env
        assert "GIT_CONFIG_VALUE_0" not in env
        assert "GIT_CONFIG_COUNT" not in env
        # GIT_TERMINAL_PROMPT 被设为 0，防止交互式挂起。
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_extra_env_overrides_host_env(
        self, allowed_root: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """适配层注入的 extra_env 优先于宿主环境（用于受控 askpass helper）。"""
        monkeypatch.setenv("GIT_ASKPASS", "host-untrusted-helper")
        cli = _make_cli(allowed_root, extra_env={"GIT_ASKPASS": "controlled-helper"})
        recorder = _FakeExecRecorder(_FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        _run(cli.run(str(git_repo), ["fetch", "origin"], 5))

        env = recorder.calls[0]["kwargs"]["env"]
        assert env["GIT_ASKPASS"] == "controlled-helper"


# --------------------------------------------------------------------------- #
# 验收 6：命令超时、输出限长和凭据脱敏生效
# --------------------------------------------------------------------------- #


class TestTimeoutAndOutputLimits:
    """命令超时、输出限长、输出脱敏。"""

    def test_timeout_raises_retryable_and_kills_process(
        self, allowed_root: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cli = _make_cli(allowed_root)
        fake_proc = _FakeProc(block=True)
        recorder = _FakeExecRecorder(fake_proc)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        with pytest.raises(ExternalDependencyError, match="timed out") as exc_info:
            _run(cli.run(str(git_repo), ["fetch", "origin"], 1))
        assert exc_info.value.retryable is True
        assert fake_proc.killed, "超时后应杀死进程"

    def test_output_truncated_to_max_bytes(
        self, allowed_root: Path, git_repo: Path
    ) -> None:
        """stdout 超过 max_output_bytes 时被截断。"""
        cli = _make_cli(allowed_root, max_output_bytes=128)

        rc, out, _err = _run(cli.run(str(git_repo), ["show", "HEAD:big.txt"], 5))
        assert rc == 0
        # 截断后长度不超过 max_output_bytes（解码后字符数 <= 字节数）。
        assert len(out) <= 128
        assert out.startswith("A" * 64)

    def test_sensitive_path_in_output_redacted(
        self, allowed_root: Path, git_repo: Path
    ) -> None:
        """输出中包含敏感路径（.ssh/id_rsa）时被脱敏。"""
        cli = _make_cli(allowed_root)

        _rc, out, _err = _run(cli.run(str(git_repo), ["ls-files"], 5))
        # .ssh/id_rsa 是敏感路径，整体行被替换为占位符。
        assert ".ssh/id_rsa" not in out
        assert "id_rsa" not in out
        assert "README.txt" in out, "非敏感路径应保留"

    def test_redact_text_helper(self, allowed_root: Path) -> None:
        """_redact_text 对敏感路径字符串脱敏。"""
        from maf_observability.redaction import REDACTED_PLACEHOLDER

        cli = _make_cli(allowed_root)
        assert cli._redact_text("path: C:\\Users\\alice\\.ssh\\id_rsa") == REDACTED_PLACEHOLDER
        assert cli._redact_text("normal text") == "normal text"


# --------------------------------------------------------------------------- #
# 端到端：真实 git 执行 + 便捷方法
# --------------------------------------------------------------------------- #


class TestRealGitExecution:
    """使用真实 git 验证白名单命令可执行且便捷方法工作。"""

    def test_status_succeeds(self, allowed_root: Path, git_repo: Path) -> None:
        cli = _make_cli(allowed_root)
        rc, out, err = _run(cli.run(str(git_repo), ["status"], 10))
        assert rc == 0
        assert "branch" in out.lower() or "tree clean" in out.lower()
        assert err == ""

    def test_rev_parse_returns_commit_hash(self, allowed_root: Path, git_repo: Path) -> None:
        cli = _make_cli(allowed_root)
        sha = _run(cli.rev_parse(str(git_repo), "HEAD"))
        assert len(sha) >= 7
        int(sha, 16)  # 合法十六进制

    def test_branch_exists_returns_true_for_main(
        self, allowed_root: Path, git_repo: Path
    ) -> None:
        cli = _make_cli(allowed_root)
        # git init 默认分支可能是 main 或 master。
        exists_main = _run(cli.branch_exists(str(git_repo), "main"))
        exists_master = _run(cli.branch_exists(str(git_repo), "master"))
        assert exists_main or exists_master

    def test_branch_exists_returns_false_for_missing(
        self, allowed_root: Path, git_repo: Path
    ) -> None:
        cli = _make_cli(allowed_root)
        assert _run(cli.branch_exists(str(git_repo), "no-such-branch")) is False

    def test_show_displays_commit(self, allowed_root: Path, git_repo: Path) -> None:
        cli = _make_cli(allowed_root)
        rc, out, _err = _run(cli.show(str(git_repo), "HEAD"))
        assert rc == 0
        assert "initial" in out

    def test_commit_creates_new_commit(self, allowed_root: Path, git_repo: Path) -> None:
        cli = _make_cli(allowed_root)
        (git_repo / "new.txt").write_text("new\n", encoding="utf-8")
        rc, _out, _err = _run(
            cli.commit(str(git_repo), "second commit", add_all=True)
        )
        assert rc == 0
        log_rc, log_out, _ = _run(cli.run(str(git_repo), ["log", "--oneline"], 10))
        assert log_rc == 0
        assert "second commit" in log_out
        assert "initial" in log_out

    def test_worktree_list_succeeds(self, allowed_root: Path, git_repo: Path) -> None:
        cli = _make_cli(allowed_root)
        rc, out, _err = _run(cli.worktree_list(str(git_repo)))
        assert rc == 0
        assert str(git_repo) in out or "repo" in out

    def test_rev_parse_failure_raises_git_command_error(
        self, allowed_root: Path, git_repo: Path
    ) -> None:
        cli = _make_cli(allowed_root)
        with pytest.raises(GitCommandError):
            _run(cli.rev_parse(str(git_repo), "no-such-ref-xyz"))


# --------------------------------------------------------------------------- #
# 适配层冒烟测试：ServerGitCli / RunnerGitCli 包装核心类
# --------------------------------------------------------------------------- #


class TestAdaptersSmoke:
    def test_server_git_cli_delegates_and_confines(
        self, allowed_root: Path, git_repo: Path
    ) -> None:
        from maf_server.gateway.repository.git_cli import ServerGitCli

        cli = ServerGitCli(git_repo_root=allowed_root)
        rc, _out, _err = _run(cli.run(str(git_repo), ["status"], 10))
        assert rc == 0
        # 越界路径同样被拒绝。
        with pytest.raises(ArgumentError):
            _run(cli.run(str(git_repo.parent.parent), ["status"], 5))

    def test_runner_git_cli_injects_token_via_env(
        self, allowed_root: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maf_runner.workspace.git import RunnerGitCli

        cli = RunnerGitCli(
            allowed_roots=[allowed_root], credential_token=_SECRET_TOKEN
        )
        recorder = _FakeExecRecorder(_FakeProc(returncode=0))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", recorder)

        _run(cli.run(str(git_repo), ["fetch", "origin"], 5))

        call = recorder.calls[0]
        for arg in call["args"]:
            assert _SECRET_TOKEN not in arg
        assert call["kwargs"]["env"]["MAF_GIT_CREDENTIAL_TOKEN"] == _SECRET_TOKEN
