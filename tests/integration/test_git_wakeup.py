import pytest
from maf_server.scheduler.wakeup import WakeupService


@pytest.mark.asyncio
async def test_wakeup_duplicate_and_failed_retry():
    calls = []
    class Scheduler:
        async def resume_run(self, run, command): calls.append(command)
    wake = WakeupService(Scheduler())
    await wake.wake("r", "e")
    await wake.wake("r", "e")
    assert len(calls) == 1

