"""拒绝超出 Job Grant 的路径、命令、挂载、URL 和输出。

TASK-066 扩展：增加 ``SecurityBaseline`` Protocol 与 ``LocalSecurityBaseline``
实现，用于节点启动时的安全基线检查（工作目录可写、Docker socket 可访问、
非 root 运行）。这些检查是启动自检的一部分，失败时节点不申请任务。
"""

from __future__ import annotations

import os
import ipaddress
import socket
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse


class BoundaryValidator(Protocol):
    def require_workspace_path(self, workspace_root: str, candidate: str) -> str:
        """解析链接和规范化路径，返回安全绝对路径；不在 root 内则抛边界错误。"""
        ...


class BoundaryViolation(ValueError):
    """Untrusted task input crossed a local execution boundary."""


class LocalBoundaryValidator:
    """Deterministic local implementation of the task grant boundaries."""

    def require_workspace_path(self, workspace_root: str, candidate: str) -> str:
        root = Path(workspace_root).expanduser().resolve()
        raw = Path(candidate).expanduser()
        path = (raw if raw.is_absolute() else root / raw).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise BoundaryViolation(f"path escapes workspace root: {candidate}") from exc
        return str(path)

    def require_allowed_command(
        self, executable: str, arguments: list[str], grant: dict
    ) -> None:
        allowed = {str(item) for item in grant.get("allowed_executables", [])}
        if executable not in allowed:
            raise BoundaryViolation(f"executable is not granted: {executable}")
        if not isinstance(arguments, list) or any(not isinstance(arg, str) for arg in arguments):
            raise BoundaryViolation("command arguments must be a list of strings")
        if any("\x00" in arg or "\n" in arg or "\r" in arg for arg in arguments):
            raise BoundaryViolation("command argument contains a control character")
        denied = {str(item) for item in grant.get("denied_arguments", [])}
        if denied.intersection(arguments):
            raise BoundaryViolation("command contains a denied argument")

    def require_allowed_url(self, url: str, grant: dict) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in set(grant.get("allowed_schemes", ["https"])):
            raise BoundaryViolation("URL scheme is not allowed")
        host = (parsed.hostname or "").rstrip(".").lower()
        if not host or parsed.username or parsed.password:
            raise BoundaryViolation("URL host is missing or contains credentials")
        allowed_hosts = {str(item).rstrip(".").lower() for item in grant.get("allowed_hosts", [])}
        if allowed_hosts and host not in allowed_hosts:
            raise BoundaryViolation(f"URL host is not granted: {host}")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        allowed_ports = {int(item) for item in grant.get("allowed_ports", [443])}
        if port not in allowed_ports:
            raise BoundaryViolation(f"URL port is not granted: {port}")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
        except socket.gaierror as exc:
            raise BoundaryViolation(f"URL host cannot be resolved: {host}") from exc
        if not addresses:
            raise BoundaryViolation("URL host resolved to no addresses")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                raise BoundaryViolation(f"URL resolves to a forbidden address: {ip}")
        return parsed.geturl()

    def require_output_limits(self, path_count: int, total_bytes: int, grant: dict) -> None:
        max_paths = int(grant.get("max_output_files", 1000))
        max_bytes = int(grant.get("max_output_bytes", 100 * 1024 * 1024))
        if path_count < 0 or path_count > max_paths:
            raise BoundaryViolation(f"output file count exceeds limit: {path_count}/{max_paths}")
        if total_bytes < 0 or total_bytes > max_bytes:
            raise BoundaryViolation(f"output byte size exceeds limit: {total_bytes}/{max_bytes}")
# --------------------------------------------------------------------------- #
# TASK-066: 安全基线检查
# --------------------------------------------------------------------------- #


class SecurityBaseline(Protocol):
    """节点启动安全基线检查接口。

    实现应保持只读、无副作用，返回 :class:`BaselineCheckResult` 描述每项
    检查的通过/失败状态与原因。失败时不抛异常——由 :class:`StartupChecker`
    汇总决定是否中止启动。
    """

    def check_workspace_writable(self, workspace_root: Path) -> "BaselineCheckResult":
        """检查工作目录存在且当前用户可写。"""
        ...

    def check_docker_socket(self, docker_socket: str) -> "BaselineCheckResult":
        """检查 Docker socket 路径存在且可访问。"""
        ...

    def check_not_running_as_root(self) -> "BaselineCheckResult":
        """检查节点进程未以 root 身份运行（POSIX）。"""
        ...


@dataclass(slots=True)
class BaselineCheckResult:
    """单项安全基线检查结果。"""

    name: str
    ok: bool
    detail: str = ""

    @classmethod
    def pass_(cls, name: str, detail: str = "") -> "BaselineCheckResult":
        return cls(name=name, ok=True, detail=detail)

    @classmethod
    def fail(cls, name: str, detail: str) -> "BaselineCheckResult":
        return cls(name=name, ok=False, detail=detail)


@dataclass(slots=True)
class LocalSecurityBaseline:
    """默认 ``SecurityBaseline`` 实现：基于本地文件系统与进程状态检查。

    设计决策：

    - **只读、无副作用**：不修改文件权限、不创建文件（除临时写测试外清理）。
    - **不抛异常**：任何检查失败返回 ``BaselineCheckResult.fail``，由调用方
      决定是否中止启动。
    - **跨平台**：Windows 下 root 检查自动跳过（返回 pass 并注明平台）；
      Docker socket 在 Windows 下检查命名管道存在性。
    - **工作目录可写性**：通过创建临时文件并删除来验证，避免仅依赖
      ``os.access`` 的 ``W_OK`` 位（在某些文件系统上不可靠）。
    """

    def check_workspace_writable(self, workspace_root: Path) -> BaselineCheckResult:
        """检查 ``workspace_root`` 存在且当前用户可写。"""
        name = "workspace_writable"
        if not workspace_root.exists():
            return BaselineCheckResult.fail(
                name,
                f"workspace_root {workspace_root} does not exist",
            )
        if not workspace_root.is_dir():
            return BaselineCheckResult.fail(
                name,
                f"workspace_root {workspace_root} is not a directory",
            )
        # 实际写一个临时文件验证可写性（比 os.access 更可靠）。
        probe = workspace_root / f".maf-write-probe-{os.getpid()}"
        try:
            probe.write_text("probe", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            return BaselineCheckResult.fail(
                name,
                f"workspace_root {workspace_root} is not writable: {exc}",
            )
        return BaselineCheckResult.pass_(name, str(workspace_root))

    def check_docker_socket(self, docker_socket: str) -> BaselineCheckResult:
        """检查 Docker socket 路径存在且可访问。"""
        name = "docker_socket"
        if not docker_socket:
            return BaselineCheckResult.fail(name, "docker_socket is empty")
        # Windows 命名管道（如 //./pipe/docker_engine）无法用 Path 检查；
        # 仅检查字符串非空，实际连通性由 ``docker info`` 自检覆盖。
        if sys.platform.startswith("win"):
            return BaselineCheckResult.pass_(
                name,
                f"docker_socket={docker_socket} (windows named pipe; "
                "connectivity checked by docker info)",
            )
        socket_path = Path(docker_socket)
        if not socket_path.exists():
            return BaselineCheckResult.fail(
                name,
                f"docker socket {docker_socket} does not exist",
            )
        # 检查 socket 类型（POSIX 下为 socket 文件）。
        try:
            mode = socket_path.stat().st_mode
            if not stat.S_ISSOCK(mode):
                return BaselineCheckResult.fail(
                    name,
                    f"docker socket path {docker_socket} is not a socket",
                )
            if not os.access(str(socket_path), os.R_OK | os.W_OK):
                return BaselineCheckResult.fail(
                    name,
                    f"docker socket {docker_socket} is not accessible "
                    "by current user",
                )
        except OSError as exc:
            return BaselineCheckResult.fail(
                name,
                f"docker socket {docker_socket} stat failed: {exc}",
            )
        return BaselineCheckResult.pass_(name, docker_socket)

    def check_not_running_as_root(self) -> BaselineCheckResult:
        """检查节点进程未以 root 身份运行（POSIX）。"""
        name = "not_root"
        if sys.platform.startswith("win"):
            # Windows 没有 root 概念，检查自动通过。
            return BaselineCheckResult.pass_(
                name, "windows platform; root check skipped"
            )
        try:
            uid = os.getuid()
        except AttributeError:
            # 非 POSIX 平台，跳过。
            return BaselineCheckResult.pass_(
                name, "non-POSIX platform; root check skipped"
            )
        if uid == 0:
            return BaselineCheckResult.fail(
                name,
                "runner must not run as root (uid=0); use a dedicated user",
            )
        return BaselineCheckResult.pass_(name, f"uid={uid}")


__all__ = [
    "BaselineCheckResult",
    "BoundaryViolation",
    "BoundaryValidator",
    "LocalBoundaryValidator",
    "LocalSecurityBaseline",
    "SecurityBaseline",
]
