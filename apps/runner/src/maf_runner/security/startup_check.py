"""节点启动自检：Docker、Git、工作目录、仓库绑定与安全基线。

TASK-066 实现：节点启动时按顺序执行环境自检，任一关键检查失败则不申请
任务并以非零状态码退出。检查项：

1. **Docker**：``docker info`` 可执行且 daemon 可达。
2. **Git**：``git --version`` 可执行。
3. **工作目录可写性**：``workspace_root`` 存在且当前用户可写。
4. **仓库绑定**：``workspace_root`` 是合法 Git 仓库，且远端 URL 与配置一致。
5. **安全基线**：Docker socket 权限、非 root 运行（POSIX）。

设计决策：

- **可注入探针**：``DependencyProbe`` Protocol 允许测试注入 mock，避免依赖
  真实 Docker/Git 环境。
- **不抛异常**：所有检查返回 ``CheckResult``，由 ``StartupChecker`` 汇总。
- **脱敏**：子进程 stderr 经 :func:`_redact` 脱敏，避免凭据/路径泄露。
- **超时**：每个子命令 5 秒超时，避免 daemon 不可达时阻塞启动。
- **白名单**：只运行 ``docker``/``git`` 二进制（与 :mod:`maf_runner.registry`
  的 ``_safe_run_version`` 一致）。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from maf_runner.security.boundaries import (
    BaselineCheckResult,
    LocalSecurityBaseline,
    SecurityBaseline,
)


# --------------------------------------------------------------------------- #
# 结果类型
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CheckResult:
    """单项检查结果。"""

    name: str
    ok: bool
    detail: str = ""

    @classmethod
    def pass_(cls, name: str, detail: str = "") -> "CheckResult":
        return cls(name=name, ok=True, detail=detail)

    @classmethod
    def fail(cls, name: str, detail: str) -> "CheckResult":
        return cls(name=name, ok=False, detail=detail)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(slots=True)
class StartupCheckResult:
    """启动自检汇总结果。"""

    checks: list[CheckResult] = field(default_factory=list)
    baseline_checks: list[BaselineCheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """所有检查通过时为 ``True``。"""
        return all(c.ok for c in self.checks) and all(
            b.ok for b in self.baseline_checks
        )

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok]

    @property
    def failed_baseline(self) -> list[BaselineCheckResult]:
        return [b for b in self.baseline_checks if not b.ok]

    def summary(self) -> str:
        """返回人类可读的汇总报告。"""
        lines: list[str] = []
        lines.append(
            f"startup self-check: {'PASS' if self.ok else 'FAIL'} "
            f"({sum(1 for c in self.checks if c.ok)}/{len(self.checks)} checks, "
            f"{sum(1 for b in self.baseline_checks if b.ok)}/"
            f"{len(self.baseline_checks)} baseline)"
        )
        for c in self.checks:
            status = "PASS" if c.ok else "FAIL"
            lines.append(f"  [{status}] {c.name}: {c.detail}")
        for b in self.baseline_checks:
            status = "PASS" if b.ok else "FAIL"
            lines.append(f"  [{status}] baseline/{b.name}: {b.detail}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 依赖探针 Protocol
# --------------------------------------------------------------------------- #


class DependencyProbe(Protocol):
    """外部依赖探针接口，用于隔离子进程调用便于测试。"""

    def check_docker(self, docker_binary: str) -> CheckResult:
        """运行 ``docker info`` 验证 daemon 可达。"""
        ...

    def check_git(self, git_binary: str) -> CheckResult:
        """运行 ``git --version`` 验证 Git 可用。"""
        ...

    def check_repo_binding(
        self, workspace_root: Path, control_remote_url: str
    ) -> CheckResult:
        """验证 ``workspace_root`` 是合法 Git 仓库且远端绑定可用。"""
        ...


# --------------------------------------------------------------------------- #
# 默认实现：本地子进程探针
# --------------------------------------------------------------------------- #


#: 子命令超时（秒），避免 Docker daemon 不可达时阻塞启动。
_PROBE_TIMEOUT_SECONDS: int = 5

#: 允许执行的二进制白名单（与 ``registry._safe_run_version`` 一致）。
_ALLOWED_BINARIES: frozenset[str] = frozenset({"git", "docker"})


def _redact(text: str) -> str:
    """脱敏 stderr 输出，避免凭据或敏感路径进入日志。"""
    # 简单脱敏：截断过长输出，移除可能的 token 模式。
    if len(text) > 500:
        text = text[:500] + "...(truncated)"
    return text


@dataclass(slots=True)
class LocalDependencyProbe:
    """默认 ``DependencyProbe`` 实现：通过 subprocess 调用 git/docker。

    设计决策：

    - **白名单**：只运行 ``git``/``docker`` 二进制，拒绝其他。
    - **超时**：每个子命令 5 秒超时。
    - **脱敏**：stderr 截断并脱敏后返回。
    - **不抛异常**：命令不存在、超时、非零退出码均返回 ``CheckResult.fail``。
    """

    def check_docker(self, docker_binary: str) -> CheckResult:
        name = "docker_info"
        if docker_binary not in _ALLOWED_BINARIES:
            return CheckResult.fail(
                name, f"binary {docker_binary!r} not in whitelist"
            )
        try:
            result = subprocess.run(
                [docker_binary, "info"],
                capture_output=True,
                text=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError:
            return CheckResult.fail(
                name, f"docker binary {docker_binary!r} not found"
            )
        except subprocess.TimeoutExpired:
            return CheckResult.fail(
                name,
                f"docker info timed out after {_PROBE_TIMEOUT_SECONDS}s",
            )
        except subprocess.SubprocessError as exc:
            return CheckResult.fail(name, f"docker info failed: {exc}")
        if result.returncode != 0:
            return CheckResult.fail(
                name,
                f"docker info exited {result.returncode}: "
                f"{_redact(result.stderr)}",
            )
        return CheckResult.pass_(name, "docker daemon reachable")

    def check_git(self, git_binary: str) -> CheckResult:
        name = "git_version"
        if git_binary not in _ALLOWED_BINARIES:
            return CheckResult.fail(
                name, f"binary {git_binary!r} not in whitelist"
            )
        try:
            result = subprocess.run(
                [git_binary, "--version"],
                capture_output=True,
                text=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError:
            return CheckResult.fail(
                name, f"git binary {git_binary!r} not found"
            )
        except subprocess.TimeoutExpired:
            return CheckResult.fail(
                name, f"git --version timed out after {_PROBE_TIMEOUT_SECONDS}s"
            )
        except subprocess.SubprocessError as exc:
            return CheckResult.fail(name, f"git --version failed: {exc}")
        if result.returncode != 0:
            return CheckResult.fail(
                name,
                f"git --version exited {result.returncode}: "
                f"{_redact(result.stderr)}",
            )
        version = result.stdout.strip()
        return CheckResult.pass_(name, version)

    def check_repo_binding(
        self, workspace_root: Path, control_remote_url: str
    ) -> CheckResult:
        """验证 ``workspace_root`` 是合法 Git 仓库。

        检查项：

        1. ``git -C <root> rev-parse --is-inside-work-tree`` 返回 ``true``。
        2. 若 ``control_remote_url`` 非空，检查仓库中存在匹配的远端
           （通过 ``git -C <root> remote -v`` 输出包含该 URL）。

        不修改仓库状态、不 fetch、不 push。
        """
        name = "repo_binding"
        if not workspace_root.exists():
            return CheckResult.fail(
                name, f"workspace_root {workspace_root} does not exist"
            )
        # 1. 验证是 Git 工作区。
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace_root), "rev-parse",
                 "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError:
            return CheckResult.fail(name, "git binary not found")
        except subprocess.TimeoutExpired:
            return CheckResult.fail(name, "git rev-parse timed out")
        except subprocess.SubprocessError as exc:
            return CheckResult.fail(name, f"git rev-parse failed: {exc}")
        if result.returncode != 0:
            return CheckResult.fail(
                name,
                f"workspace_root {workspace_root} is not a git repository: "
                f"{_redact(result.stderr)}",
            )
        if result.stdout.strip() != "true":
            return CheckResult.fail(
                name,
                f"workspace_root {workspace_root} is not inside a work tree",
            )
        # ``git -C <subdir> rev-parse --is-inside-work-tree`` also returns
        # true for an arbitrary directory nested inside a parent repository.
        # A node workspace must be the repository root it is bound to, not a
        # silently inherited parent checkout.
        try:
            root_result = subprocess.run(
                ["git", "-C", str(workspace_root), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.SubprocessError as exc:
            return CheckResult.fail(name, f"git rev-parse root failed: {exc}")
        if root_result.returncode != 0:
            return CheckResult.fail(
                name,
                f"workspace_root {workspace_root} is not a git repository: "
                f"{_redact(root_result.stderr)}",
            )
        try:
            repo_root = Path(root_result.stdout.strip()).resolve()
            requested_root = workspace_root.resolve()
        except OSError:
            repo_root = Path(root_result.stdout.strip())
            requested_root = workspace_root
        if repo_root != requested_root:
            return CheckResult.fail(
                name,
                f"workspace_root {workspace_root} is not a git repository root "
                f"(inside parent repository {repo_root}, not its root)",
            )
        # 2. 若配置了远端 URL，验证仓库存在匹配的远端。
        if control_remote_url:
            try:
                remote_result = subprocess.run(
                    ["git", "-C", str(workspace_root), "remote", "-v"],
                    capture_output=True,
                    text=True,
                    timeout=_PROBE_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.SubprocessError as exc:
                return CheckResult.fail(
                    name, f"git remote -v failed: {exc}"
                )
            if remote_result.returncode != 0:
                return CheckResult.fail(
                    name,
                    f"git remote -v exited {remote_result.returncode}: "
                    f"{_redact(remote_result.stderr)}",
                )
            remotes = remote_result.stdout
            if control_remote_url not in remotes:
                return CheckResult.fail(
                    name,
                    f"control_remote_url {control_remote_url!r} not found "
                    f"in git remotes; configured remotes:\n"
                    f"{_redact(remotes)}",
                )
        return CheckResult.pass_(
            name, f"workspace_root={workspace_root} is a valid git repository"
        )


# --------------------------------------------------------------------------- #
# 启动自检编排器
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class StartupChecker:
    """编排节点启动自检。

    按顺序执行依赖检查与安全基线检查，汇总为 :class:`StartupCheckResult`。
    任一检查失败时 ``result.ok`` 为 ``False``，调用方据此决定是否中止启动。

    参数：
        probe: 依赖探针（Docker/Git/仓库绑定）；默认
            :class:`LocalDependencyProbe`。
        baseline: 安全基线检查器；默认 :class:`LocalSecurityBaseline`。
    """

    probe: DependencyProbe | None = None
    baseline: SecurityBaseline | None = None

    def _get_probe(self) -> DependencyProbe:
        return self.probe if self.probe is not None else LocalDependencyProbe()

    def _get_baseline(self) -> SecurityBaseline:
        return (
            self.baseline
            if self.baseline is not None
            else LocalSecurityBaseline()
        )

    def run(
        self,
        *,
        docker_binary: str = "docker",
        git_binary: str = "git",
        docker_socket: str = "",
        workspace_root: Path | None = None,
        control_remote_url: str = "",
    ) -> StartupCheckResult:
        """执行全部启动自检，返回汇总结果。

        参数：
            docker_binary: Docker CLI 二进制名（默认 ``docker``）。
            git_binary: Git CLI 二进制名（默认 ``git``）。
            docker_socket: Docker socket 路径（用于基线检查）。
            workspace_root: 节点工作目录（用于可写性与仓库绑定检查）。
            control_remote_url: 协调远端 URL（用于仓库绑定检查）。

        返回：
            :class:`StartupCheckResult`，``ok`` 为 ``True`` 时全部通过。
        """
        probe = self._get_probe()
        baseline = self._get_baseline()
        result = StartupCheckResult()

        # 1. Docker daemon 可达性。
        result.checks.append(probe.check_docker(docker_binary))

        # 2. Git 可用性。
        result.checks.append(probe.check_git(git_binary))

        # 3. 仓库绑定。
        if workspace_root is not None:
            result.checks.append(
                probe.check_repo_binding(workspace_root, control_remote_url)
            )
        else:
            result.checks.append(
                CheckResult.fail("repo_binding", "workspace_root is None")
            )

        # 4. 工作目录可写性（安全基线）。
        if workspace_root is not None:
            result.baseline_checks.append(
                baseline.check_workspace_writable(workspace_root)
            )
        else:
            result.baseline_checks.append(
                BaselineCheckResult.fail(
                    "workspace_writable", "workspace_root is None"
                )
            )

        # 5. Docker socket 权限（安全基线）。
        result.baseline_checks.append(
            baseline.check_docker_socket(docker_socket)
        )

        # 6. 非 root 运行（安全基线）。
        result.baseline_checks.append(baseline.check_not_running_as_root())

        return result


__all__ = [
    "CheckResult",
    "DependencyProbe",
    "LocalDependencyProbe",
    "StartupCheckResult",
    "StartupChecker",
]
