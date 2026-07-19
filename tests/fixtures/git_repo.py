"""本地 Git 仓库 fixture：用真实 git 初始化，不依赖 GitHub。

提供 ``LocalGitRepo`` 句柄封装常用 git 命令，以及 ``init_local_git_repo``
工厂。所有操作使用本地 ``git`` 二进制和临时目录，不触碰远端仓库、
GitHub API 或真实凭据，满足「测试不依赖真实 GitHub」的验收标准。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_AUTHOR = ("Test Bot", "bot@example.test")


def _git_env() -> dict[str, str]:
    """构造隔离的 git 身份环境，避免继承宿主 user.name/email。"""
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": _DEFAULT_AUTHOR[0],
        "GIT_AUTHOR_EMAIL": _DEFAULT_AUTHOR[1],
        "GIT_COMMITTER_NAME": _DEFAULT_AUTHOR[0],
        "GIT_COMMITTER_EMAIL": _DEFAULT_AUTHOR[1],
    }


@dataclass
class LocalGitRepo:
    """本地 Git 仓库句柄，封装常用 git 子命令。"""

    path: Path
    _env: dict[str, str] = field(default_factory=_git_env, repr=False)

    def run(self, args: list[str], *, cwd: Path | None = None) -> str:
        """执行 git 子命令并返回 stdout；失败抛 CalledProcessError。"""
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.path),
            env=self._env,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def add(self, *paths: str) -> None:
        self.run(["add", "--", *paths])

    def commit(self, message: str) -> str:
        return self.run(["commit", "-q", "-m", message])

    def write_file(self, name: str, content: str) -> Path:
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def commit_file(self, name: str, content: str, message: str | None = None) -> str:
        """写入文件、暂存并提交，返回 commit stdout。"""
        self.write_file(name, content)
        self.add(name)
        return self.commit(message or f"add {name}")

    def rev_parse(self, ref: str = "HEAD") -> str:
        return self.run(["rev-parse", ref]).strip()

    def current_branch(self) -> str:
        return self.run(["rev-parse", "--abbrev-ref", "HEAD"]).strip()

    def checkout_branch(self, branch: str, *, create: bool = False) -> None:
        args = ["checkout"]
        if create:
            args.append("-b")
        args.append(branch)
        self.run(args)


def init_local_git_repo(path: Path, *, initial_commit: bool = True) -> LocalGitRepo:
    """在 ``path`` 下初始化一个本地 git 仓库。

    - 不使用 GitHub，仅本地 ``git init``；
    - 设置隔离的 user.name/user.email，避免宿主身份污染；
    - ``initial_commit=True`` 时创建一个初始 README 提交，便于后续 rev_parse HEAD。
    """
    path.mkdir(parents=True, exist_ok=True)
    env = _git_env()
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", _DEFAULT_AUTHOR[1]],
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", _DEFAULT_AUTHOR[0]],
        check=True,
        env=env,
    )
    # 显式指定默认分支为 main，避免不同 git 版本默认分支差异。
    subprocess.run(
        ["git", "-C", str(path), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
        env=env,
    )
    repo = LocalGitRepo(path=path)
    if initial_commit:
        repo.commit_file("README.md", "# test repo\n", "initial commit")
    return repo


__all__ = ["LocalGitRepo", "init_local_git_repo"]
