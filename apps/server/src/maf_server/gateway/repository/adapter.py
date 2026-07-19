"""TASK-035: 基于 SubprocessGitCli 的 GitRepositoryAdapter 实现。

实现 :class:`RepositoryAdapter` 协议，提供 ``verify``、``list_branches``、
``get_default_branch`` 三个方法。

设计要点（对应任务验收标准）：

1. **统一健康结果**：``verify`` 返回 :class:`VerifyResult`，包含
   ``verified``、``repository_info``（default_branch、branches、can_read、
   can_write）和 ``error``；GitHub 与本地 Git 走同一路径。
2. **凭据不复制进绑定表**：本适配器只接收 ``credentials`` dict（含已解析的
   token 或 SSH key 路径），凭据经 ``extra_env`` 注入子进程，不进入命令行参数、
   不进入日志。明文由调用方（RepositoryBindingService）从 SecretService 解析后
   短暂传入，用完由 GC 释放。
3. **验证只做安全探测**：使用 ``clone --bare``（只读探测，不创建工作树）、
   ``for-each-ref``（列出分支）、``rev-parse --symbolic-full-name HEAD``（获取
   默认分支）、``push --dry-run``（探测写权限，不修改远端）。不修改主分支。
4. **白名单约束**：``clone``、``fetch``、``push``、``for-each-ref``、
   ``show-ref``、``rev-parse`` 均在 :data:`SubprocessGitCli.ALLOWED_SUBCOMMANDS` 中。
   ``ls-remote`` 和 ``symbolic-ref`` 不在白名单中，故使用 ``clone --bare`` +
   ``for-each-ref``/``rev-parse`` 替代。
"""

from __future__ import annotations

import re
import shutil
import stat
import uuid
import os
from pathlib import Path
from typing import Any

import structlog

from maf_repository_adapters import SubprocessGitCli

from .service import VerifyResult

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: ``verify`` 是 SecretService 默认允许的 resolve purpose。
_VERIFY_PURPOSE: str = "verify"

#: SSH key 路径禁止出现的 shell 元字符（防止 GIT_SSH_COMMAND 注入）。
_SSH_PATH_FORBIDDEN_CHARS: frozenset[str] = frozenset(
    {";", "|", "&", "$", "`", "(", ")", "\n", "\r", " ", "\t"}
)

#: 临时验证分支前缀。
_VERIFY_BRANCH_PREFIX: str = "_maf_verify_"


class GitRepositoryAdapter:
    """``RepositoryAdapter`` 的 Git 实现（TASK-035）。

    使用 :class:`SubprocessGitCli` 执行本地 git 命令，凭据经 ``extra_env`` 注入
    子进程环境，绝不进入命令行参数或日志。

    构造参数：
        workspace_root: 临时 clone 目录的父目录；所有 clone 产物在此目录下创建
            并在验证结束后清理。
        default_timeout_seconds: git 命令超时秒数。
        logger: structlog logger；为 ``None`` 时按模块名创建。
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        default_timeout_seconds: int = 60,
        logger: Any = None,
    ) -> None:
        self._workspace_root: Path = Path(workspace_root)
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._timeout: int = max(1, default_timeout_seconds)
        self._log: Any = logger or structlog.get_logger("maf.repository_adapter")

    # ------------------------------------------------------------------ #
    # RepositoryAdapter Protocol 实现
    # ------------------------------------------------------------------ #

    async def verify(
        self,
        repository_url: str,
        credentials: dict,
        *,
        expected_branch: str | None = None,
    ) -> VerifyResult:
        """无破坏验证仓库可访问性、分支存在性和写权限。

        流程：
        1. 构建凭据 env（HTTPS token 或 SSH key）。
        2. ``git clone --bare <url> <temp_dir>`` 探测读权限（不创建工作树）。
        3. clone 失败 → 返回 ``VerifyResult(False, None, error)``。
        4. ``git rev-parse --symbolic-full-name HEAD`` 获取默认分支。
        5. ``git for-each-ref --format=%(refname:short) refs/heads/`` 列出分支。
        6. 若 ``expected_branch`` 指定，检查其在分支列表中。
        7. ``git push --dry-run origin <branch>:refs/heads/_maf_verify_*`` 探测写权限。
        8. 清理临时目录，返回 :class:`VerifyResult`。
        """
        if not repository_url or not isinstance(repository_url, str):
            return VerifyResult(
                verified=False, repository_info=None,
                error="repository_url must be a non-empty string",
            )

        clone_dir = self._workspace_root / f"{_VERIFY_BRANCH_PREFIX}{uuid.uuid4().hex}"
        cli = self._build_cli(credentials)

        try:
            # 1. clone --bare 探测读权限（不创建工作树）
            rc_clone, _out, err_clone = await cli.run(
                str(self._workspace_root),
                ["clone", "--bare", "--", repository_url, str(clone_dir)],
                self._timeout,
            )
            if rc_clone != 0:
                self._log.info(
                    "repository_verify_clone_failed",
                    repository_url=self._redact_url(repository_url),
                    error_preview=err_clone[:200] if err_clone else "",
                )
                return VerifyResult(
                    verified=False,
                    repository_info=None,
                    error=f"clone failed: {err_clone.strip() if err_clone else 'unknown error'}",
                )

            # 2. 获取默认分支（rev-parse --symbolic-full-name HEAD）
            default_branch = await self._get_default_branch_inner(cli, str(clone_dir))
            if default_branch is None:
                return VerifyResult(
                    verified=False,
                    repository_info=None,
                    error="failed to resolve default branch (HEAD not a symbolic ref)",
                )

            # 3. 列出分支
            branches = await self._list_branches_inner(cli, str(clone_dir))

            # 4. 检查 expected_branch
            if expected_branch is not None and expected_branch not in branches:
                return VerifyResult(
                    verified=False,
                    repository_info={
                        "default_branch": default_branch,
                        "branches": branches,
                        "can_read": True,
                        "can_write": False,
                    },
                    error=f"expected branch {expected_branch!r} not found; "
                    f"available: {branches[:10]}",
                )

            # 5. 探测写权限（push --dry-run 到临时验证分支）
            check_branch = expected_branch or default_branch
            can_write = await self._check_write_permission(
                cli, str(clone_dir), repository_url, check_branch
            )

            self._log.info(
                "repository_verify_success",
                repository_url=self._redact_url(repository_url),
                default_branch=default_branch,
                branch_count=len(branches),
                can_write=can_write,
            )
            return VerifyResult(
                verified=True,
                repository_info={
                    "default_branch": default_branch,
                    "branches": branches,
                    "can_read": True,
                    "can_write": can_write,
                },
                error=None,
            )

        finally:
            # 清理临时 clone 目录
            self._cleanup_clone_dir(clone_dir)

    async def list_branches(
        self, repository_url: str, credentials: dict
    ) -> list[str]:
        """列出仓库远端分支名。``clone --bare`` + ``for-each-ref``。"""
        clone_dir = self._workspace_root / f"{_VERIFY_BRANCH_PREFIX}{uuid.uuid4().hex}"
        cli = self._build_cli(credentials)
        try:
            rc, _out, err = await cli.run(
                str(self._workspace_root),
                ["clone", "--bare", "--", repository_url, str(clone_dir)],
                self._timeout,
            )
            if rc != 0:
                raise RuntimeError(
                    f"clone failed: {err.strip() if err else 'unknown error'}"
                )
            return await self._list_branches_inner(cli, str(clone_dir))
        finally:
            self._cleanup_clone_dir(clone_dir)

    async def get_default_branch(
        self, repository_url: str, credentials: dict
    ) -> str:
        """返回仓库默认分支名。``clone --bare`` + ``rev-parse``。"""
        clone_dir = self._workspace_root / f"{_VERIFY_BRANCH_PREFIX}{uuid.uuid4().hex}"
        cli = self._build_cli(credentials)
        try:
            rc, _out, err = await cli.run(
                str(self._workspace_root),
                ["clone", "--bare", "--", repository_url, str(clone_dir)],
                self._timeout,
            )
            if rc != 0:
                raise RuntimeError(
                    f"clone failed: {err.strip() if err else 'unknown error'}"
                )
            branch = await self._get_default_branch_inner(cli, str(clone_dir))
            if branch is None:
                raise RuntimeError("failed to resolve default branch")
            return branch
        finally:
            self._cleanup_clone_dir(clone_dir)

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _build_cli(self, credentials: dict) -> SubprocessGitCli:
        """构建注入凭据 env 的 SubprocessGitCli。"""
        extra_env = self._build_credential_env(credentials)
        return SubprocessGitCli(
            allowed_roots=[self._workspace_root],
            default_timeout_seconds=self._timeout,
            extra_env=extra_env,
        )

    def _build_credential_env(self, credentials: dict) -> dict[str, str]:
        """从 credentials dict 构建子进程凭据 env。值不记录到日志。

        - HTTPS_TOKEN：token 注入 ``MAF_GIT_CREDENTIAL_TOKEN``。
        - SSH_KEY：校验 key 路径后构造 ``GIT_SSH_COMMAND=ssh -i <path> ...``。
        - NONE：无凭据 env（用于本地 file:// 仓库）。
        """
        env: dict[str, str] = {}
        cred_type = credentials.get("type", "NONE")
        if cred_type == "HTTPS_TOKEN":
            token = credentials.get("token", "")
            if token:
                env["MAF_GIT_CREDENTIAL_TOKEN"] = token
        elif cred_type == "SSH_KEY":
            ssh_key_path = credentials.get("ssh_key_path", "")
            if ssh_key_path:
                self._validate_ssh_key_path(ssh_key_path)
                env["GIT_SSH_COMMAND"] = (
                    "ssh -o IdentitiesOnly=yes -o BatchMode=yes "
                    f"-o StrictHostKeyChecking=accept-new "
                    f"-i {ssh_key_path}"
                )
        return env

    def _validate_ssh_key_path(self, path: str) -> None:
        """校验 SSH key 路径：必须绝对、存在、是文件，不含 shell 元字符。"""
        if not path:
            raise ValueError("ssh_key_path must not be empty")
        for ch in _SSH_PATH_FORBIDDEN_CHARS:
            if ch in path:
                raise ValueError(
                    f"ssh_key_path contains forbidden characters: {path!r}"
                )
        p = Path(path)
        if not p.is_absolute():
            raise ValueError(f"ssh_key_path must be an absolute path: {path!r}")
        if not p.is_file():
            raise ValueError(
                f"ssh_key_path does not exist or is not a regular file: {path!r}"
            )

    async def _get_default_branch_inner(
        self, cli: SubprocessGitCli, repo_path: str
    ) -> str | None:
        """``git rev-parse --symbolic-full-name HEAD`` → ``refs/heads/main``。"""
        rc, out, _err = await cli.run(
            repo_path,
            ["rev-parse", "--symbolic-full-name", "HEAD"],
            self._timeout,
        )
        if rc != 0:
            return None
        ref = out.strip()
        # ``refs/heads/main`` → ``main``
        if ref.startswith("refs/heads/"):
            return ref[len("refs/heads/"):]
        return ref if ref else None

    async def _list_branches_inner(
        self, cli: SubprocessGitCli, repo_path: str
    ) -> list[str]:
        """``git for-each-ref --format=%(refname:short) refs/heads/``。"""
        rc, out, _err = await cli.run(
            repo_path,
            ["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
            self._timeout,
        )
        if rc != 0:
            return []
        return sorted(line.strip() for line in out.splitlines() if line.strip())

    async def _check_write_permission(
        self,
        cli: SubprocessGitCli,
        repo_path: str,
        repository_url: str,
        branch: str,
    ) -> bool:
        """``git push --dry-run origin <branch>:refs/heads/_maf_verify_*``。

        ``--dry-run`` 保证不修改远端。使用现有分支推送到临时验证分支名，
        远端不会真正创建分支。
        """
        verify_branch = f"{_VERIFY_BRANCH_PREFIX}{uuid.uuid4().hex}"
        rc, _out, err = await cli.run(
            repo_path,
            [
                "push",
                "--dry-run",
                "--",
                repository_url,
                f"refs/heads/{branch}:refs/heads/{verify_branch}",
            ],
            self._timeout,
        )
        if rc == 0:
            return True
        self._log.info(
            "repository_verify_push_denied",
            repository_url=self._redact_url(repository_url),
            error_preview=err[:200] if err else "",
        )
        return False

    @staticmethod
    def _cleanup_clone_dir(clone_dir: Path) -> None:
        """Remove a temporary bare clone, including read-only Git objects.

        Git may create read-only pack/object files on Windows.  A plain
        ``shutil.rmtree(..., ignore_errors=True)`` silently leaves those
        directories behind, which both leaks disk state and breaks repeated
        verification.  Clear the write bit and retry removal on any failure.
        """
        if not clone_dir.exists():
            return

        def _make_writable(func: Any, path: str, _exc: Any) -> None:
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                func(path)
            except OSError:
                # A later retry handles transient Windows file locks.
                return

        shutil.rmtree(clone_dir, onerror=_make_writable)
        if clone_dir.exists():
            shutil.rmtree(clone_dir, ignore_errors=True)

    @staticmethod
    def _redact_url(url: str) -> str:
        """脱敏 URL 中的凭据片段，用于日志。"""
        redacted = re.sub(
            r"(https?://)[^@/:]+:[^@/:]+@",
            r"\1***@",
            url,
        )
        redacted = re.sub(
            r"(https?://)[^@/:]+@",
            r"\1***@",
            redacted,
        )
        return redacted


__all__ = ["GitRepositoryAdapter"]
