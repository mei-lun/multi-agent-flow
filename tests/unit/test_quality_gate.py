import pytest
from maf_server.scheduler.graph_nodes.evaluate import evaluate_node
from maf_server.scheduler.state import RunState


@pytest.mark.asyncio
async def test_blocking_gate_failure_cannot_be_overridden():
    class Gate:
        def evaluate(self, *args, **kwargs):
            return {"id": "d", "passed": True, "gate_results": [{"blocking": True, "passed": False}]}
    state = await evaluate_node(RunState("r", "w", current_node_ids=["n"]), "n", "a", Gate())
    assert state.status == "FAILED"

