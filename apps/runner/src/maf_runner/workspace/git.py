"""代码任务 worktree、分支元数据和 Patch 输出接口。

本文件提供：

- :class:`GitWorkspace`：节点工作区 Protocol（prepare/collect/cleanup）。
- :class:`RunnerGitCli`：节点端 GitCli 适配，包装
  :class:`maf_repository_adapters.SubprocessGitCli`，绑定
  ``NodeSettings.workspace_root`` 与 ``local_git_roots``，并将
  ``git_credentials_token`` 经环境变量注入子进程（不进入命令行参数或日志）。

安全保证全部由 :class:`SubprocessGitCli` 提供：参数数组执行（无 ``shell=True``）、
子命令白名单、危险标志拒绝、路径限制在 ``workspace_root``/``local_git_roots``、
凭据经环境变量传递、命令超时、输出限长与脱敏。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from maf_repository_adapters import SubprocessGitCli
from maf_runner.security.boundaries import BoundaryViolation, LocalBoundaryValidator

if TYPE_CHECKING:
    from maf_runner.config import NodeSettings


class GitWorkspace(Protocol):
    async def prepare(self, job_id: str, source_artifact_version_id: str, base_commit: str, expected_tree_hash: str, writable_subpaths: list[str]) -> str:
        """在新目录导入 bundle/archive，校验 commit/tree，创建本地 worktree；不配置远端凭据。"""
        ...


class LocalGitWorkspace:
    """Concrete isolated clone workspace for one assignment epoch."""

    def __init__(
        self,
        *,
        git_cli: object,
        workspace_root: Path,
        node_id: str,
        assignment_epoch: int,
    ) -> None:
        self._git = git_cli
        self._root = Path(workspace_root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._node_id = node_id
        self._epoch = assignment_epoch
        self._validator = LocalBoundaryValidator()
        self._writable: dict[str, tuple[str, ...]] = {}

    async def _run(self, cwd: Path, args: list[str]) -> str:
        result = await self._git.run(str(cwd), args, 0)
        rc, out, err = result
        if rc != 0:
            raise RuntimeError(f"git {' '.join(args[:2])} failed: {err.strip()}")
        return out.strip()

    async def prepare(
        self,
        job_id: str,
        source_artifact_version_id: str,
        base_commit: str,
        expected_tree_hash: str,
        writable_subpaths: list[str],
    ) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", job_id):
            raise BoundaryViolation("unsafe job_id")
        if self._epoch < 1 or not self._node_id:
            raise BoundaryViolation("node_id and positive assignment epoch are required")
        workspace = Path(
            self._validator.require_workspace_path(str(self._root), f"git-{job_id}-{self._epoch}")
        )
        if workspace.exists():
            shutil.rmtree(workspace)
        source_path = Path(source_artifact_version_id).expanduser()
        if source_path.is_symlink():
            raise BoundaryViolation("source repository may not be a symlink")
        source = source_path.resolve()
        if not source.is_dir():
            raise BoundaryViolation("source repository does not exist")
        await self._run(self._root, ["clone", "--no-checkout", str(source), str(workspace)])
        await self._run(workspace, ["checkout", "--detach", base_commit])
        actual_tree = await self._run(workspace, ["rev-parse", f"{base_commit}^{{tree}}"])
        if expected_tree_hash and actual_tree != expected_tree_hash:
            shutil.rmtree(workspace)
            raise BoundaryViolation("base commit tree hash does not match assignment")
        branch = f"maf/task/{job_id}/{self._epoch}/{self._node_id}"
        await self._run(workspace, ["switch", "-c", branch])
        normalized: list[str] = []
        for relative in writable_subpaths:
            path = Path(self._validator.require_workspace_path(str(workspace), relative))
            normalized.append(path.relative_to(workspace).as_posix())
        self._writable[str(workspace)] = tuple(normalized)
        return str(workspace)

    async def collect(self, workspace_path: str) -> dict:
        workspace = Path(self._validator.require_workspace_path(str(self._root), workspace_path))
        allowed = self._writable.get(str(workspace), ())
        # 展开未跟踪目录，否则 porcelain 默认只报告 ``?? outputs/``，会丢失
        # 实际发生变更的文件路径并削弱 writable_subpaths 审计精度。
        status = await self._run(
            workspace,
            ["status", "--porcelain", "--untracked-files=all"],
        )
        changed: list[str] = []
        for line in status.splitlines():
            if not line:
                continue
            path = line[3:].strip().split(" -> ")[-1]
            candidate = Path(self._validator.require_workspace_path(str(workspace), path))
            relative = candidate.relative_to(workspace).as_posix()
            if allowed and not any(relative == prefix or relative.startswith(f"{prefix}/") for prefix in allowed):
                raise BoundaryViolation(f"changed path is outside writable grant: {relative}")
            changed.append(relative)
        head = await self._run(workspace, ["rev-parse", "HEAD"])
        tree = await self._run(workspace, ["rev-parse", "HEAD^{tree}"])
        branch = await self._run(workspace, ["rev-parse", "--abbrev-ref", "HEAD"])
        return {
            "workspace_path": str(workspace),
            "branch": branch,
            "head_commit": head,
            "tree_hash": tree,
            "changed_paths": sorted(set(changed)),
        }

    async def cleanup(self, workspace_path: str) -> None:
        workspace = Path(self._validator.require_workspace_path(str(self._root), workspace_path))
        if workspace == self._root:
            raise BoundaryViolation("refusing to delete workspace root")
        self._writable.pop(str(workspace), None)
        if workspace.exists():
            shutil.rmtree(workspace)

#: 节点凭据 token 注入的环境变量名。平台私有，由节点 askpass helper
#: （TASK-014 实现）读取。该变量值是 SecretStr，绝不进入命令行参数或日志。
_GIT_CREDENTIAL_TOKEN_ENV: str = "MAF_GIT_CREDENTIAL_TOKEN"


class RunnerGitCli:
    """Runner 端 GitCli 适配，绑定节点工作区根目录与本地 Git 根。

    谁调用它：节点 ``git_client.py``、``workspace/generic.py`` 等在需要
    fetch/push 协调分支或操作任务工作区时调用。

    设计决策：
    - ``allowed_roots`` 为 ``NodeSettings.workspace_root`` 与
      ``local_git_roots`` 的并集；所有 git 操作被限制在这些根目录内。
    - ``git_credentials_token``（SecretStr）经环境变量注入子进程，绝不进入
      命令行参数；askpass helper 读取该环境变量完成认证（helper 装配在
      TASK-014）。核心类保证环境变量值不进入日志。
    - 复用核心类的白名单、超时、输出限长与脱敏策略。
    """

    def __init__(
        self,
        *,
        allowed_roots: list[Path],
        credential_token: str | None = None,
        default_timeout_seconds: int = 60,
        max_output_bytes: int = 1024 * 1024,
        git_binary: str = "git",
    ) -> None:
        extra_env: dict[str, str] = {}
        if credential_token:
            extra_env[_GIT_CREDENTIAL_TOKEN_ENV] = credential_token
        self._inner = SubprocessGitCli(
            allowed_roots=allowed_roots,
            default_timeout_seconds=default_timeout_seconds,
            max_output_bytes=max_output_bytes,
            extra_env=extra_env,
            git_binary=git_binary,
        )

    @classmethod
    def from_settings(
        cls,
        settings: NodeSettings,
    ) -> RunnerGitCli:
        """从 ``NodeSettings`` 构造。

        ``allowed_roots`` 取 ``workspace_root`` 与 ``local_git_roots`` 并集；
        ``git_credentials_token``（SecretStr）解包为字符串注入环境变量。
        """
        roots: list[Path] = [settings.workspace_root, *settings.local_git_roots]
        token_secret = settings.git_credentials_token
        token = token_secret.get_secret_value() if token_secret is not None else None
        return cls(
            allowed_roots=roots,
            credential_token=token,
            default_timeout_seconds=settings.poll_interval_seconds,
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


__all__ = ["GitWorkspace", "LocalGitWorkspace", "RunnerGitCli"]
