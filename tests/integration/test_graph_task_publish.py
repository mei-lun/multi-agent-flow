import pytest
from maf_server.scheduler.dispatcher import DispatchRequest, TaskDispatcher


@pytest.mark.asyncio
async def test_dispatch_is_idempotent_and_contains_contracts():
    published = []
    class Coordination:
        async def publish_tasks(self, project, tasks, expected): published.extend(tasks)
    dispatcher = TaskDispatcher(Coordination(), project_id="p")
    request = DispatchRequest("r", "n", "role", "repo", ("python",), "a" * 40, output_contracts=({"key": "out"},))
    first = await dispatcher.dispatch(request)
    assert first == await dispatcher.dispatch(request)
    assert published[0]["requirements"]["output_contracts"] == [{"key": "out"}]

