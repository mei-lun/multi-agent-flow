import pytest

from maf_server.modules.workflows.repository import InMemoryWorkflowRepository
from maf_server.modules.workflows.service import WorkflowServiceImpl


ACTOR = {"user_id": "u1", "organization_id": "o1", "permission_keys": [], "trace_id": "t"}


@pytest.mark.asyncio
async def test_definition_version_copy_is_independent_and_key_is_unique():
    service = WorkflowServiceImpl()
    workflow = await service.create_workflow(ACTOR, {"key": "build", "name": "Build", "description": "", "idempotency_key": "w1"})
    first = await service.create_version(ACTOR, workflow["id"], {"based_on_version_id": None, "change_summary": "initial", "idempotency_key": "v1"})
    graph = {"start_node_key": "end", "nodes": [{"key": "end", "kind": "END_SUCCESS", "input_contracts": [], "output_contracts": [], "retry_policy": {}, "timeout_seconds": 1, "ui_position": {}}], "edges": []}
    await service.save_graph(ACTOR, first["id"], {"graph": graph, "expected_version": 1, "idempotency_key": "g1"})
    copied = await service.create_version(ACTOR, workflow["id"], {"based_on_version_id": first["id"], "change_summary": "copy", "idempotency_key": "v2"})
    copied_graph = await service.repository.load_graph(copied["id"])
    copied_graph["nodes"][0]["ui_position"]["x"] = 99
    assert (await service.repository.load_graph(first["id"]))["nodes"][0]["ui_position"] == {}

