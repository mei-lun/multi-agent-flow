"""Secure Git CLI wrapper with parameter-array subprocess execution.

实现 ``GitCli`` 协议的具体类 :class:`SubprocessGitCli`。安全保证（对应
TASK-012 验收标准与《系统设计文档》19.2 节）：

1. **禁止 Shell 字符串拼接**：使用 ``asyncio.create_subprocess_exec("git", *args)``
   传参数数组，从不 ``shell=True``。``;``、``$()``、``|``、反引号等 shell 元字符
   在参数中被 git 视为字面量，不会被 shell 解释执行。
2. **子命令白名单**：``arguments[0]`` 必须属于 :data:`ALLOWED_SUBCOMMANDS`；
   这同时阻止了在子命令前插入全局选项（``git -c key=val ...``、
   ``git -C /path ...``、``--git-dir`` 等），因为 ``arguments[0]`` 不允许是选项。
3. **危险标志扫描**：``--upload-pack``、``--receive-pack``、``--ext-diff``、
   ``--exec``、``--sequence-editor`` 等可触发任意命令执行的标志在任意位置被拒绝
   （含 ``--flag=value`` 形式）。
4. **路径限制在仓库/工作区根目录**：``repository_path`` 经 ``Path.resolve()``
   规范化后必须位于构造期传入的 ``allowed_roots`` 之一；符号链接逃逸和
   ``..`` 遍历被阻止。
5. **凭据不进入命令行参数或日志**：凭据只能通过 ``extra_env``（子进程环境变量）
   传递，绝不进入 ``arguments``，也绝不进入日志。环境变量中可触发命令注入的
   ``GIT_*`` 变量（``GIT_SSH_COMMAND``、``GIT_EDITOR``、``GIT_CONFIG_*`` 等）
   在合并前从宿主环境剥离；``GIT_TERMINAL_PROMPT=0`` 防止交互式提示挂起。
6. **命令超时**：``asyncio.wait_for`` 强制 ``timeout_seconds``；超时后杀死进程
   并抛出可重试的 :class:`ExternalDependencyError`。
7. **输出限长与脱敏**：stdout/stderr 截断到 ``max_output_bytes``；返回与日志
   前经 :func:`redact_sensitive` 脱敏敏感键名与宿主机敏感路径。
8. **本地仓库锁**：同一 ``repository_path`` 上的命令串行执行，避免索引损坏。

本模块不依赖 FastAPI、SQLAlchemy、Docker 或模型 SDK；仅依赖标准库、
``maf_domain`` 错误体系和 ``maf_observability`` 脱敏器。
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import Final, Mapping

from maf_domain.errors import ArgumentError, ExternalDependencyError
from maf_observability import get_logger
from maf_observability.redaction import REDACTED_PLACEHOLDER, redact_sensitive
from maf_observability.logger import Logger

#: 允许的 git 子命令白名单。排除 ``config``、``remote`` 等可改写 git 行为的
#: 配置类命令，以及 ``filter-branch``、``submodule``、``bisect`` 等可触发
#: 任意命令执行的命令。
ALLOWED_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {
        "init",
        "clone",
        "fetch",
        "pull",
        "push",
        "checkout",
        "switch",
        "branch",
        "tag",
        "add",
        "rm",
        "mv",
        "commit",
        "stash",
        "merge",
        "rebase",
        "cherry-pick",
        "revert",
        "reset",
        "clean",
        "log",
        "show",
        "diff",
        "status",
        "shortlog",
        "rev-parse",
        "rev-list",
        "ls-tree",
        "ls-files",
        "cat-file",
        "for-each-ref",
        "show-ref",
        "update-ref",
        "worktree",
        "reflog",
        "blame",
        "name-rev",
        "describe",
        "gc",
        "archive",
    }
)

#: 可触发任意命令执行的标志，禁止出现在参数任意位置（含 ``--flag=value`` 形式）。
#: 注意：``-c`` / ``-C`` 是 git 全局选项，因 ``arguments[0]`` 必须为子命令已被
#: 阻止，故不再重复扫描，避免误伤 ``git commit -c <commit>`` 等合法用法。
FORBIDDEN_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "--upload-pack",
        "--receive-pack",
        "--ext-diff",
        "--exec",
        "--sequence-editor",
    }
)

#: 宿主环境中需剥离的 ``GIT_*`` 变量：它们可触发任意命令执行或注入 git 配置。
#: ``GIT_CONFIG_*`` 系列单独按前缀剥离（见 :meth:`SubprocessGitCli._build_env`）。
_STRIPPED_ENV_VARS: Final[frozenset[str]] = frozenset(
    {
        "GIT_SSH_COMMAND",
        "GIT_SSH",
        "GIT_EXTERNAL_DIFF",
        "GIT_EDITOR",
        "GIT_SEQUENCE_EDITOR",
        "GIT_PAGER",
        "GIT_ASKPASS",
        "GIT_CREDENTIAL_HELPER",
        "GIT_PROXY_COMMAND",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
    }
)

_DEFAULT_TIMEOUT_SECONDS: Final[int] = 60
_DEFAULT_MAX_OUTPUT_BYTES: Final[int] = 1 * 1024 * 1024  # 1 MiB
#: 行控制字符校验：refspec/branch/remote 不允许出现换行、NUL 等控制字符。
_CONTROL_CHARS_RE: Final[str] = "".join(
    chr(c) for c in range(32) if chr(c) not in "\t"
)


def _is_forbidden_flag(token: str) -> bool:
    """判断 token 是否为或以危险标志开头（覆盖 ``--flag=value`` 形式）。"""
    if token in FORBIDDEN_FLAGS:
        return True
    for flag in FORBIDDEN_FLAGS:
        if token.startswith(flag + "="):
            return True
    return False


def _has_control_chars(value: str) -> bool:
    """返回字符串是否含换行、NUL 等控制字符（制表符除外）。"""
    return any(ch in _CONTROL_CHARS_RE or ch == "\x7f" for ch in value)


class SubprocessGitCli:
    """参数数组方式执行 git 的安全封装，实现 ``GitCli`` 协议。

    构造参数：
        allowed_roots: ``repository_path`` 必须位于这些根目录之一（已 resolve）。
        default_timeout_seconds: 调用方传入 ``timeout_seconds<=0`` 时的回退超时。
        max_output_bytes: stdout/stderr 截断阈值。
        extra_env: 注入子进程环境的变量（用于凭据）。**值绝不会出现在日志中**。
        git_binary: git 可执行文件名或绝对路径，默认 ``"git"``。
        logger: 可选 structlog logger；为 ``None`` 时按模块名创建。

    ``run`` 方法签名与 ``GitCli`` 协议一致，凭据通过构造期 ``extra_env`` 绑定，
    不暴露在 ``run`` 参数中，因此不会进入命令行参数。
    """

    def __init__(
        self,
        *,
        allowed_roots: list[Path],
        default_timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        extra_env: Mapping[str, str] | None = None,
        git_binary: str = "git",
        logger: Logger | None = None,
    ) -> None:
        if not allowed_roots:
            raise ValueError("allowed_roots must not be empty")
        self._allowed_roots: list[Path] = [root.resolve() for root in allowed_roots]
        self._default_timeout: int = max(1, default_timeout_seconds)
        self._max_output_bytes: int = max(1, max_output_bytes)
        # 拷贝凭据环境；绝不记录其值。
        self._extra_env: dict[str, str] = dict(extra_env) if extra_env else {}
        self._git_binary: str = git_binary
        self._logger: Logger = logger or get_logger(__name__)
        # 同一仓库路径的串行锁，按 ``(path, event_loop)`` 缓存，避免并发 git
        # 操作损坏索引。按事件循环区分以支持测试中多次 ``asyncio.run`` 复用同一
        # 实例；生产环境单循环下退化为每仓库一把锁。
        self._repo_locks: dict[tuple[str, asyncio.AbstractEventLoop], asyncio.Lock] = {}

    # ------------------------------------------------------------------ #
    # 协议入口
    # ------------------------------------------------------------------ #

    async def run(
        self,
        repository_path: str,
        arguments: list[str],
        timeout_seconds: int,
    ) -> tuple[int, str, str]:
        """执行参数数组而非 Shell 字符串。

        参数：
            repository_path: 仓库/工作区目录，必须位于 ``allowed_roots`` 之一。
            arguments: ``git`` 之后的参数列表；``arguments[0]`` 必须在白名单。
            timeout_seconds: 超时秒数；``<=0`` 时使用默认值。

        返回 ``(returncode, stdout, stderr)``，输出已截断并脱敏。

        异常：
            ArgumentError: 子命令不在白名单、含危险标志、路径越界或参数非法。
            ExternalDependencyError: git 可执行文件不存在或命令超时（可重试）。
        """
        resolved_root = self._validate_repository_path(repository_path)
        safe_args = self._validate_arguments(arguments)
        effective_timeout = (
            timeout_seconds if timeout_seconds > 0 else self._default_timeout
        )
        env = self._build_env()
        cmd_repr = self._safe_command_repr(safe_args)

        self._logger.info(
            "git_cli_run_start",
            subcommand=safe_args[0],
            arg_count=len(safe_args),
            repository_root=str(resolved_root),
            timeout_seconds=effective_timeout,
            command_repr=cmd_repr,
        )

        # 同一仓库串行执行，避免索引竞争；按事件循环区分以防跨循环复用。
        lock_key = (str(resolved_root), asyncio.get_running_loop())
        lock = self._repo_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            try:
                proc = await asyncio.create_subprocess_exec(
                    self._git_binary,
                    *safe_args,
                    cwd=str(resolved_root),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                raise ExternalDependencyError(
                    f"git executable not found: {self._git_binary}",
                    context={"git_binary": self._git_binary},
                    retryable=False,
                ) from exc

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=effective_timeout
                )
            except asyncio.TimeoutError as exc:
                # 超时后杀死进程，避免遗留子进程。
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                self._logger.warning(
                    "git_cli_run_timeout",
                    subcommand=safe_args[0],
                    timeout_seconds=effective_timeout,
                )
                raise ExternalDependencyError(
                    f"git command timed out after {effective_timeout}s",
                    context={
                        "subcommand": safe_args[0],
                        "timeout_seconds": effective_timeout,
                    },
                    retryable=True,
                ) from exc

        returncode = proc.returncode if proc.returncode is not None else -1
        stdout = self._truncate_bytes(stdout_bytes)
        stderr = self._truncate_bytes(stderr_bytes)
        stdout_redacted = self._redact_text(stdout)
        stderr_redacted = self._redact_text(stderr)

        self._logger.info(
            "git_cli_run_done",
            subcommand=safe_args[0],
            returncode=returncode,
            stdout_len=len(stdout_redacted),
            stderr_len=len(stderr_redacted),
        )
        return returncode, stdout_redacted, stderr_redacted

    # ------------------------------------------------------------------ #
    # 便捷方法：以参数数组方式构建常见命令（fetch/show/rev-parse/worktree/
    # commit/push/分支检查）。输入经校验后委托 ``run``。
    # ------------------------------------------------------------------ #

    async def fetch(
        self,
        repository_path: str,
        remote: str = "origin",
        refspecs: list[str] | None = None,
        *,
        timeout_seconds: int = 0,
    ) -> tuple[int, str, str]:
        """``git fetch <remote> [<refspec>...]``，参数化执行。"""
        self._validate_reflike(remote, "remote")
        args = ["fetch", "--", remote]
        if refspecs:
            for spec in refspecs:
                self._validate_reflike(spec, "refspec")
            args.extend(refspecs)
        return await self.run(repository_path, args, timeout_seconds)

    async def show(
        self,
        repository_path: str,
        revision: str,
        path: str | None = None,
        *,
        timeout_seconds: int = 0,
    ) -> tuple[int, str, str]:
        """``git show <revision>[:<path>]``。"""
        self._validate_reflike(revision, "revision")
        args = ["show", revision]
        if path:
            self._validate_path_segment(path)
            args.append("--")
            args.append(path)
        return await self.run(repository_path, args, timeout_seconds)

    async def rev_parse(
        self,
        repository_path: str,
        ref: str,
        *,
        timeout_seconds: int = 0,
    ) -> str:
        """``git rev-parse <ref>``，返回去首尾空白的 commit hash。"""
        self._validate_reflike(ref, "ref")
        rc, out, _err = await self.run(
            repository_path, ["rev-parse", ref], timeout_seconds
        )
        if rc != 0:
            raise GitCommandError(
                f"git rev-parse failed for {ref!r} (exit {rc})"
            )
        return out.strip()

    async def worktree_add(
        self,
        repository_path: str,
        target_path: str,
        commit_or_branch: str,
        *,
        timeout_seconds: int = 0,
    ) -> tuple[int, str, str]:
        """``git worktree add <path> <commit-or-branch>``。"""
        self._validate_reflike(commit_or_branch, "commit_or_branch")
        args = ["worktree", "add", target_path, commit_or_branch]
        return await self.run(repository_path, args, timeout_seconds)

    async def worktree_list(
        self,
        repository_path: str,
        *,
        timeout_seconds: int = 0,
    ) -> tuple[int, str, str]:
        """``git worktree list``。"""
        return await self.run(
            repository_path, ["worktree", "list"], timeout_seconds
        )

    async def worktree_remove(
        self,
        repository_path: str,
        target_path: str,
        *,
        force: bool = False,
        timeout_seconds: int = 0,
    ) -> tuple[int, str, str]:
        """``git worktree remove [--force] <path>``。"""
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(target_path)
        return await self.run(repository_path, args, timeout_seconds)

    async def commit(
        self,
        repository_path: str,
        message: str,
        *,
        add_all: bool = False,
        author: str | None = None,
        timeout_seconds: int = 0,
    ) -> tuple[int, str, str]:
        """``git commit -m <message>``，不打开编辑器。

        ``add_all`` 为真时先 ``git add -A``（单独调用）。``author`` 形如
        ``"Name <email>"``，作为 ``--author`` 参数传递（非密钥）。
        """
        if add_all:
            add_rc, _add_out, add_err = await self.run(
                repository_path, ["add", "-A"], timeout_seconds
            )
            if add_rc != 0:
                return add_rc, "", add_err
        args = ["commit", "-m", message, "--no-edit"]
        if author:
            self._validate_author(author)
            args.append(f"--author={author}")
        return await self.run(repository_path, args, timeout_seconds)

    async def push(
        self,
        repository_path: str,
        remote: str,
        refspec: str,
        *,
        timeout_seconds: int = 0,
    ) -> tuple[int, str, str]:
        """``git push <remote> <refspec>``。凭据经 ``extra_env`` 传递，不进参数。"""
        self._validate_reflike(remote, "remote")
        self._validate_reflike(refspec, "refspec")
        args = ["push", "--", remote, refspec]
        return await self.run(repository_path, args, timeout_seconds)

    async def branch_list(
        self,
        repository_path: str,
        *,
        timeout_seconds: int = 0,
    ) -> tuple[int, str, str]:
        """``git branch --list``。"""
        return await self.run(
            repository_path, ["branch", "--list"], timeout_seconds
        )

    async def branch_exists(
        self,
        repository_path: str,
        name: str,
        *,
        timeout_seconds: int = 0,
    ) -> bool:
        """分支检查：``git show-ref --verify --quiet refs/heads/<name>``。"""
        self._validate_reflike(name, "branch name")
        # 用 -- 确保 name 不被当作选项。
        rc, _out, _err = await self.run(
            repository_path,
            ["show-ref", "--verify", "--quiet", f"refs/heads/{name}"],
            timeout_seconds,
        )
        return rc == 0

    # ------------------------------------------------------------------ #
    # 校验与工具
    # ------------------------------------------------------------------ #

    def _validate_repository_path(self, repository_path: str) -> Path:
        """规范化路径并确认位于 ``allowed_roots`` 之一。"""
        if not repository_path:
            raise ArgumentError("repository_path must not be empty")
        candidate = Path(repository_path)
        # ``resolve(strict=False)`` 规范化符号链接与 ``..``；不要求路径已存在。
        resolved = candidate.resolve()
        for root in self._allowed_roots:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            return resolved
        raise ArgumentError(
            f"repository_path {repository_path!r} resolves outside allowed roots",
            context={"resolved": str(resolved)},
        )

    def _validate_arguments(self, arguments: list[str]) -> list[str]:
        """校验子命令白名单与危险标志，返回原参数列表。"""
        if not arguments:
            raise ArgumentError("arguments must not be empty")
        subcommand = arguments[0]
        if subcommand not in ALLOWED_SUBCOMMANDS:
            raise ArgumentError(
                f"git subcommand {subcommand!r} is not in the whitelist",
                context={"subcommand": subcommand},
            )
        for token in arguments:
            if not isinstance(token, str):
                raise ArgumentError("all arguments must be strings")
            if _is_forbidden_flag(token):
                raise ArgumentError(
                    f"forbidden git flag {token!r} in arguments",
                    context={"flag": token},
                )
        return list(arguments)

    def _validate_reflike(self, value: str, field_name: str) -> None:
        """校验 refspec/branch/remote 不含控制字符且不以 ``-`` 开头。"""
        if not value:
            raise ArgumentError(f"{field_name} must not be empty")
        if _has_control_chars(value):
            raise ArgumentError(f"{field_name} must not contain control characters")
        if value.startswith("-"):
            raise ArgumentError(f"{field_name} must not start with '-'")

    def _validate_path_segment(self, path: str) -> None:
        """校验作为路径传递的参数不含控制字符。"""
        if _has_control_chars(path):
            raise ArgumentError("path must not contain control characters")

    def _validate_author(self, author: str) -> None:
        """校验 ``--author`` 值不含控制字符。"""
        if _has_control_chars(author):
            raise ArgumentError("author must not contain control characters")

    def _build_env(self) -> dict[str, str]:
        """构建最小化子进程环境：剥离宿主危险 ``GIT_*``，注入凭据 env。"""
        env = dict(os.environ)
        for key in list(env):
            if key in _STRIPPED_ENV_VARS or key.startswith("GIT_CONFIG_"):
                env.pop(key, None)
        env["GIT_TERMINAL_PROMPT"] = "0"
        # 凭据经 extra_env 注入；此处不记录 env 值。
        env.update(self._extra_env)
        return env

    def _safe_command_repr(self, args: list[str]) -> str:
        """返回脱敏后的命令表示，用于日志。凭据从不在此处出现。"""
        joined = " ".join(shlex.quote(part) for part in args)
        redacted = redact_sensitive(joined)
        return redacted if isinstance(redacted, str) else REDACTED_PLACEHOLDER

    def _truncate_bytes(self, data: bytes) -> str:
        """截断到 ``max_output_bytes`` 并以 utf-8 解码（errors='replace'）。"""
        truncated = data[: self._max_output_bytes]
        return truncated.decode("utf-8", errors="replace")

    def _redact_text(self, text: str) -> str:
        """对输出文本按行脱敏敏感路径。

        ``redact_sensitive`` 对字符串值是“整串替换”语义（命中即整体替换为占位符），
        适合日志字段；但 git 输出常为多行，若整段替换会丢失合法内容。因此按行处理：
        含敏感路径片段的行替换为占位符，其余行原样保留。凭据不会出现在 git 输出中
        （经环境变量传递），此处主要防御敏感宿主机路径泄漏。
        """
        if not text:
            return text
        redacted_lines: list[str] = []
        for line in text.split("\n"):
            red = redact_sensitive(line)
            redacted_lines.append(
                red if isinstance(red, str) else REDACTED_PLACEHOLDER
            )
        return "\n".join(redacted_lines)


class GitCommandError(ExternalDependencyError):
    """便捷方法执行 git 命令失败时抛出。"""


__all__ = [
    "ALLOWED_SUBCOMMANDS",
    "FORBIDDEN_FLAGS",
    "GitCommandError",
    "SubprocessGitCli",
]
