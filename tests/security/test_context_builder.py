import pytest

from maf_runner.runtime.context_builder import LocalContextBuilder
from maf_runner.security.boundaries import BoundaryViolation


def _envelope():
    return {
        "project_id": "p1", "task_id": "TASK-1", "assignment_id": "assignment-1",
        "assignment_epoch": 2, "based_on_control_commit": "a" * 40,
        "task_type": "CODE", "role_version_ref": {
            "status": "PUBLISHED", "assignment_epoch": 2,
            "based_on_control_commit": "a" * 40, "base_commit": "b" * 40,
            "skill_version_ids": ["skill-v1"], "tool_keys": ["echo"], "model_alias": "primary",
        },
        "input_refs": ["artifact-large"], "output_contract": {"type": "object"},
        "resource_profile": "generic", "docker_image_digest": "image@sha256:" + "c" * 64,
        "workspace": {"kind": "GIT", "repository_path": "", "work_branch": "", "base_commit": "b" * 40, "writable_subpaths": ["outputs"]},
        "network_policy_ref": {}, "capability_policy_ref": {}, "timeout_seconds": 30,
        "max_steps": 3, "max_tool_calls": 2,
        "budget": {"max_input_tokens": 10, "max_output_tokens": 10, "max_cost_amount": "1", "currency": "USD"},
    }


@pytest.mark.asyncio
async def test_context_contains_only_envelope_authorized_capabilities(tmp_path):
    builder = LocalContextBuilder(
        skill_client=object(), tool_registry={"echo": {"key": "echo"}, "admin": {"key": "admin"}},
        model_registry={"primary": object(), "extra": object()}, workspace_root=str(tmp_path),
    )
    result = await builder.build(_envelope(), str(tmp_path))
    assert result["tools"] == [{"key": "echo"}]
    assert result["model_alias"] == "primary"
    assert result["input_handles"] == [{"artifact_version_id": "artifact-large"}]


@pytest.mark.asyncio
async def test_context_rejects_snapshot_epoch_or_base_mismatch(tmp_path):
    envelope = _envelope()
    envelope["role_version_ref"]["assignment_epoch"] = 1
    builder = LocalContextBuilder(skill_client=object(), tool_registry={"echo": {}}, model_registry={"primary": object()}, workspace_root=str(tmp_path))
    with pytest.raises(BoundaryViolation):
        await builder.build(envelope, str(tmp_path))
