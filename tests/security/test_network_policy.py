import socket

import pytest

from maf_runner.security.boundaries import BoundaryViolation, LocalBoundaryValidator
from maf_runner.docker.network import LocalNetworkPolicyApplier


def test_url_policy_rejects_private_resolution(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(BoundaryViolation):
        LocalBoundaryValidator().require_allowed_url(
            "https://example.test/path",
            {"allowed_hosts": ["example.test"], "allowed_ports": [443]},
        )


def test_url_policy_accepts_granted_public_address(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    result = LocalBoundaryValidator().require_allowed_url(
        "https://example.com/path",
        {"allowed_hosts": ["example.com"], "allowed_ports": [443]},
    )
    assert result == "https://example.com/path"


@pytest.mark.asyncio
async def test_network_policy_defaults_to_offline_and_cleanup_is_idempotent():
    applier = LocalNetworkPolicyApplier()
    handle = await applier.prepare({})
    assert handle["mode"] == "OFFLINE"
    assert handle["network_mode"] == "none"
    await applier.cleanup(handle)
    await applier.cleanup(handle)
