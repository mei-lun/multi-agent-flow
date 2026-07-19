import pytest
from maf_domain.errors import VersionConflictError
from maf_server.modules.workflows.service import WorkflowServiceImpl


@pytest.mark.asyncio
async def test_graph_hash_is_stable_and_stale_save_does_not_overwrite():
    actor = {"user_id": "u", "organization_id": "o", "permission_keys": [], "trace_id": ""}
    service = WorkflowServiceImpl()
    workflow = await service.create_workflow(actor, {"key": "k", "name": "n", "description": "", "idempotency_key": "1"})
    version = await service.create_version(actor, workflow["id"], {"based_on_version_id": None, "change_summary": "", "idempotency_key": "2"})
    graph = {"start_node_key": "e", "nodes": [{"key": "e", "kind": "END_SUCCESS", "input_contracts": [], "output_contracts": [], "retry_policy": {}, "timeout_seconds": 1, "ui_position": {}}], "edges": []}
    saved = await service.save_graph(actor, version["id"], {"graph": graph, "expected_version": 1, "idempotency_key": "3"})
    with pytest.raises(VersionConflictError):
        await service.save_graph(actor, version["id"], {"graph": {**graph, "start_node_key": "e"}, "expected_version": 1, "idempotency_key": "4"})
    assert saved["graph_hash"] == (await service.repository.get_version(version["id"]))["graph_hash"]

