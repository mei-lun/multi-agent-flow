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

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from maf_repository_adapters import SubprocessGitCli

if TYPE_CHECKING:
    from maf_runner.config import NodeSettings


class GitWorkspace(Protocol):
    async def prepare(self, job_id: str, source_artifact_version_id: str, base_commit: str, expected_tree_hash: str, writable_subpaths: list[str]) -> str:
        """在新目录导入 bundle/archive，校验 commit/tree，创建本地 worktree；不配置远端凭据。"""
        ...

    async def collect(self, workspace_path: str) -> dict:
        """检查改动未越过允许路径，生成 Patch、可选 bundle、producer commit 和 tree hash。"""
        ...

    async def cleanup(self, workspace_path: str) -> None:
        """路径必须在 Runner workspace root，清理前停止使用它的容器。"""
        ...


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


__all__ = ["GitWorkspace", "RunnerGitCli"]
