"""TASK-098 security integration examples (all secrets are synthetic)."""

from __future__ import annotations

from pathlib import Path

import pytest

from maf_runner.security.boundaries import BoundaryViolation, LocalBoundaryValidator
from maf_server.gateway.policy.capability_policy import build_empty_capability_policy_service


def test_path_command_and_network_bypass_are_rejected(tmp_path: Path) -> None:
    validator = LocalBoundaryValidator()
    with pytest.raises(BoundaryViolation):
        validator.require_workspace_path(str(tmp_path), "../../etc/passwd")
    with pytest.raises(BoundaryViolation):
        validator.require_allowed_command("sh", ["-c", "curl https://evil.test"], {"allowed_executables": ["git"]})
    with pytest.raises(BoundaryViolation):
        validator.require_allowed_url("http://127.0.0.1:8000/admin", {"allowed_hosts": ["127.0.0.1"], "allowed_ports": [8000], "allowed_schemes": ["http"]})


@pytest.mark.asyncio
async def test_missing_capability_policy_fails_closed() -> None:
    service = build_empty_capability_policy_service()
    decision = await service.evaluate({
        "actor_id": "synthetic-user",
        "actor_roles": ["OBSERVER"],
        "capability_type": "tool",
        "capability_name": "shell.exec",
        "capability_version": "1",
        "context": {},
    })
    assert decision["allowed"] is False
    assert decision["reason"] == "DEFAULT_DENY"


def test_security_fixture_never_contains_production_secret() -> None:
    synthetic = "test-only-secret-do-not-use-in-production"
    assert "sk-live" not in synthetic
    assert "production" in synthetic
