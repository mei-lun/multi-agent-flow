import pytest
from maf_server.modules.workflows.service import WorkflowServiceImpl


@pytest.mark.asyncio
async def test_publish_has_content_hash_and_stable_diff():
    actor = {"user_id": "u", "organization_id": "o", "permission_keys": [], "trace_id": ""}
    events = []
    service = WorkflowServiceImpl(event_publisher=events.append)
    workflow = await service.create_workflow(actor, {"key": "k", "name": "n", "description": "", "idempotency_key": "1"})
    version = await service.create_version(actor, workflow["id"], {"based_on_version_id": None, "change_summary": "", "idempotency_key": "2"})
    graph = {"start_node_key": "e", "nodes": [{"key": "e", "kind": "END_SUCCESS", "input_contracts": [], "output_contracts": [], "retry_policy": {}, "timeout_seconds": 1, "ui_position": {}}], "edges": []}
    await service.save_graph(actor, version["id"], {"graph": graph, "expected_version": 1, "idempotency_key": "3"})
    published = await service.publish(actor, version["id"], {"expected_version": 2, "idempotency_key": "4"})
    assert published["content_hash"] and events

