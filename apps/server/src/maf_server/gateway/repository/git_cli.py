"""Git 白名单子进程接口。

本文件同时提供：

- :class:`GitCli`：server 网关层 ``GitCli`` 协议（保持向后兼容）。
- :class:`ServerGitCli`：绑定 ``ServerSettings.git_repo_root`` 的具体实现，
  包装 :class:`maf_repository_adapters.SubprocessGitCli`，作为 server 端
  Repository Gateway 执行本地 git 命令的唯一入口。

安全保证全部由 :class:`SubprocessGitCli` 提供：参数数组执行（无 ``shell=True``）、
子命令白名单、危险标志拒绝、路径限制在 ``git_repo_root``、凭据经环境变量传递
且不进入命令行参数或日志、命令超时、输出限长与脱敏。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from maf_repository_adapters import SubprocessGitCli

if TYPE_CHECKING:
    from maf_server.config import ServerSettings


class GitCli(Protocol):
    async def run(self, repository_path: str, arguments: list[str], timeout_seconds: int) -> tuple[int, str, str]:
        """执行参数数组而非 Shell 字符串。

        repository_path 必须通过受控根目录检查；arguments 首项在允许子命令集合；禁止从用户
        输入拼接 `-c`、hook、external diff、credential helper 等配置；输出限长并脱敏。
        """
        ...


class ServerGitCli:
    """Server 端 GitCli 适配，绑定 ``ServerSettings.git_repo_root``。

    谁调用它：server 端 Repository Gateway（``gateway/repository/service.py`` 等）
    在需要读写本地协调仓库时调用。

    设计决策：
    - ``allowed_roots`` 固定为 ``ServerSettings.git_repo_root``，所有 git 操作
      被限制在该协调仓库根目录内。
    - 凭据（如 GitHub token）通过 ``extra_env`` 注入子进程环境，绝不进入命令行
      参数；具体凭据装配在 TASK-014 ``RepositoryGateway.verify_binding`` 中完成。
    - 复用核心类的白名单、超时、输出限长与脱敏策略，不在适配层重复实现安全逻辑。
    """

    def __init__(
        self,
        *,
        git_repo_root: Path,
        extra_env: dict[str, str] | None = None,
        default_timeout_seconds: int = 60,
        max_output_bytes: int = 1024 * 1024,
        git_binary: str = "git",
    ) -> None:
        self._inner = SubprocessGitCli(
            allowed_roots=[git_repo_root],
            default_timeout_seconds=default_timeout_seconds,
            max_output_bytes=max_output_bytes,
            extra_env=extra_env,
            git_binary=git_binary,
        )

    @classmethod
    def from_settings(
        cls,
        settings: ServerSettings,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> ServerGitCli:
        """从 ``ServerSettings`` 构造，绑定 ``git_repo_root``。"""
        return cls(
            git_repo_root=settings.git_repo_root,
            extra_env=extra_env,
        )

    @property
    def inner(self) -> SubprocessGitCli:
        """暴露底层核心类，供需要便捷方法（fetch/show/commit/push 等）的调用方使用。"""
        return self._inner

    async def run(
        self,
        repository_path: str,
        arguments: list[str],
        timeout_seconds: int,
    ) -> tuple[int, str, str]:
        """委托底层 :class:`SubprocessGitCli.run`。"""
        return await self._inner.run(repository_path, arguments, timeout_seconds)

    # ------------------------------------------------------------------ #
    # TASK-019: read-only Git helpers for event discovery
    # ------------------------------------------------------------------ #

    async def list_files_in_branch(
        self,
        repository_path: str,
        branch: str,
        prefix: str,
        *,
        timeout_seconds: int = 0,
    ) -> list[str]:
        """``git ls-tree -r --name-only <branch> -- <prefix>``: list files under a prefix on a branch.

        Read-only: does not modify the working tree, switch branches, or push.
        Returns an empty list when the directory is missing or empty so callers
        can gracefully handle node branches that have not yet created events.
        Paths are returned relative to the repo root (including ``prefix``)
        and sorted lexicographically.
        """
        rc, out, _err = await self._inner.run(
            repository_path,
            ["ls-tree", "-r", "--name-only", branch, "--", prefix],
            timeout_seconds,
        )
        if rc != 0:
            return []
        paths = [line.strip() for line in out.splitlines() if line.strip()]
        return sorted(paths)

    async def read_file_from_branch(
        self,
        repository_path: str,
        branch: str,
        path: str,
        *,
        timeout_seconds: int = 0,
    ) -> tuple[int, str, str]:
        """``git show <branch>:<path>``: read a file from a branch without checkout.

        Read-only: does not switch the working tree or modify any file.
        Returns ``(rc, stdout, stderr)``; callers check ``rc == 0`` for success.
        A missing file yields ``rc != 0`` with an explanatory ``stderr``.
        """
        return await self._inner.run(
            repository_path,
            ["show", f"{branch}:{path}"],
            timeout_seconds,
        )

    async def list_branches(
        self,
        repository_path: str,
        pattern: str,
        *,
        timeout_seconds: int = 0,
    ) -> list[str]:
        """``git for-each-ref --format=%(refname:short) refs/heads/<pattern>``: list matching local branches.

        Read-only: only enumerates ref names, does not modify any refs.
        Returns short branch names (e.g. ``maf/node/<node-id>``) sorted
        lexicographically. Returns an empty list when nothing matches.
        """
        rc, out, _err = await self._inner.run(
            repository_path,
            [
                "for-each-ref",
                "--format=%(refname:short)",
                f"refs/heads/{pattern}",
            ],
            timeout_seconds,
        )
        if rc != 0:
            return []
        return sorted(line.strip() for line in out.splitlines() if line.strip())


__all__ = ["GitCli", "ServerGitCli"]
