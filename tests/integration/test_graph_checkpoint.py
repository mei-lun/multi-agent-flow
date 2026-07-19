"""TASK-060 checkpoint and fallback graph behavior."""

from pathlib import Path

from maf_server.scheduler.checkpointer import create_checkpointer
from maf_server.scheduler.graph_builder import compile_workflow
from maf_server.scheduler.state import RunState


def _workflow():
    return {
        "content_hash": "workflow-hash-1",
        "start_node_key": "agent",
        "nodes": [
            {
                "key": "agent",
                "kind": "AGENT",
                "role_version_id": "role-v1",
                "input_contracts": [],
                "output_contracts": [],
                "retry_policy": {"max_retries": 0},
                "timeout_seconds": 30,
            },
            {"key": "done", "kind": "END_SUCCESS", "input_contracts": [], "output_contracts": []},
        ],
        "edges": [{"key": "edge", "source_node_key": "agent", "target_node_key": "done", "condition": None, "priority": 0}],
    }


class _Dispatcher:
    async def dispatch(self, request):
        return "TASK-ABC-1"


def test_fallback_checkpointer_round_trip_and_waits_at_git_task(tmp_path: Path):
    checkpointer = create_checkpointer(str(tmp_path / "checkpoints.db"))
    workflow = _workflow()
    workflow["dispatcher"] = _Dispatcher()
    graph = compile_workflow(workflow, checkpointer)
    config = {"configurable": {"thread_id": "run-1"}}
    result = graph.invoke(RunState("run-1", "wf-1").to_dict(), config=config)
    assert result["status"] == "WAITING_GIT_TASK"
    restored = checkpointer.get(config)
    assert restored["waiting_for"] == "TASK-ABC-1"
    assert "role_version_id" in restored["metadata"]["nodes"]["agent"]

