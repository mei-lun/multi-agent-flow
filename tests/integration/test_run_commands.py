import pytest
from maf_server.modules.runs.repository import InMemoryRunRepository
from maf_server.modules.runs.service import RunServiceImpl


@pytest.mark.asyncio
async def test_pause_persists_before_scheduler_and_is_idempotent():
    repo = InMemoryRunRepository()
    run = {"id": "r", "project_id": "p", "workflow_version_id": "w", "snapshot_artifact_version_id": "s", "status": "RUNNING", "limits": {"budget_amount": "1", "currency": "USD", "token_budget": 1, "max_tasks": 1, "max_reworks": 0, "max_run_seconds": 1}, "consumed": {}, "started_at": None, "completed_at": None, "failure_code": None, "version": 1}
    await repo.create_run_with_snapshot(run, {"project": {}, "project_input": {}, "repository_binding": None, "workflow_version": {}, "workflow_graph": {}, "role_versions": [], "skill_versions": [], "tool_versions": [], "model_policies": [], "control_base_commit": "", "limits": run["limits"], "created_by": "u", "created_at": "now"}, idempotency_key="x", request_hash="h")
    calls = []
    class Scheduler:
        async def pause_run(self, run, command): calls.append(run)
    service = RunServiceImpl(repo, scheduler=Scheduler())
    actor = {"user_id": "u", "organization_id": "o", "permission_keys": [], "trace_id": ""}
    req = {"reason": "x", "expected_version": 1, "idempotency_key": "c"}
    await service.pause(actor, "r", req)
    await service.pause(actor, "r", req)
    assert calls == ["r"] and (await repo.get_run("r"))["status"] == "PAUSED"

