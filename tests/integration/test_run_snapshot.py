import pytest
from maf_server.modules.runs.repository import InMemoryRunRepository
from maf_server.modules.runs.service import RunServiceImpl


class Projects:
    async def get_project(self, project_id): return {"id": project_id, "status": "ACTIVE", "name": "p"}
    async def get_input_version(self, input_id): return {"id": input_id, "project_id": "p1", "name": "r"}


class Workflows:
    async def get_version(self, version_id): return {"id": version_id, "status": "PUBLISHED", "content_hash": "h"}
    async def load_graph(self, version_id): return {"start_node_key": "end", "nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_start_run_is_idempotent_and_snapshot_is_deep_copy():
    service = RunServiceImpl(project_source=Projects(), input_source=Projects(), workflow_source=Workflows())
    actor = {"user_id": "u", "organization_id": "o", "permission_keys": [], "trace_id": ""}
    request = {"workflow_version_id": "w1", "project_input_version_id": "i1", "repository_binding_id": None, "limits": {"budget_amount": "10", "currency": "USD", "token_budget": 100, "max_tasks": 2, "max_reworks": 1, "max_run_seconds": 100}, "idempotency_key": "same"}
    first = await service.start_run(actor, "p1", request)
    second = await service.start_run(actor, "p1", request)
    assert first["id"] == second["id"]
    snapshot = await service.repository.get_snapshot(first["id"])
    snapshot["project"]["name"] = "changed"
    assert (await service.repository.get_snapshot(first["id"]))["project"]["name"] == "p"

