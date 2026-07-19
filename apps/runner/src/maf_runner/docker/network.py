"""Per-job container network policy preparation and cleanup."""

from typing import Protocol
from uuid import uuid4

from maf_runner.security.boundaries import BoundaryViolation, LocalBoundaryValidator


class NetworkPolicyApplier(Protocol):
    async def prepare(self, policy_ref: dict) -> dict:
        """从已拉取的不可变策略文件解析允许域名/IP/端口；默认无外网，防止 DNS 重绑定和私网访问。"""
        ...

    async def cleanup(self, network_handle: dict) -> None:
        """删除本 Job 网络规则，不影响其他容器。"""
        ...


class LocalNetworkPolicyApplier:
    """Validate immutable network grants and track job-scoped rule handles.

    The returned handle is consumed by the Docker adapter.  It defaults to an
    internal network with no egress; adapters must never translate ``offline``
    to Docker's host or bridge network.
    """

    def __init__(self) -> None:
        self._active: dict[str, dict] = {}
        self._validator = LocalBoundaryValidator()

    async def prepare(self, policy_ref: dict) -> dict:
        if not isinstance(policy_ref, dict):
            raise BoundaryViolation("network policy must be an object")
        mode = str(policy_ref.get("mode", "OFFLINE")).upper()
        if mode not in {"OFFLINE", "ALLOWLIST", "APPROVED_EXTERNAL"}:
            raise BoundaryViolation(f"unsupported network policy mode: {mode}")
        endpoints = policy_ref.get("endpoints", [])
        if not isinstance(endpoints, list):
            raise BoundaryViolation("network endpoints must be a list")
        normalized: list[str] = []
        if mode != "OFFLINE":
            grant = {
                "allowed_schemes": policy_ref.get("allowed_schemes", ["https"]),
                "allowed_hosts": policy_ref.get("allowed_hosts", []),
                "allowed_ports": policy_ref.get("allowed_ports", [443]),
            }
            normalized = [self._validator.require_allowed_url(str(url), grant) for url in endpoints]
        handle_id = f"net-{uuid4().hex}"
        handle = {
            "id": handle_id,
            "mode": mode,
            "network_mode": "none" if mode == "OFFLINE" else "maf-egress-proxy",
            "allowed_endpoints": normalized,
        }
        self._active[handle_id] = handle
        return dict(handle)

    async def cleanup(self, network_handle: dict) -> None:
        handle_id = str(network_handle.get("id", "")) if isinstance(network_handle, dict) else ""
        if handle_id:
            self._active.pop(handle_id, None)


__all__ = ["LocalNetworkPolicyApplier", "NetworkPolicyApplier"]
