import pytest
from maf_server.scheduler.service import SchedulerServiceImpl


@pytest.mark.asyncio
async def test_recovery_skips_normal_git_wait():
    class Runs:
        async def list_incomplete_runs(self): return [{"id": "r", "status": "WAITING_GIT_TASK"}]
    scheduler = SchedulerServiceImpl(run_repository=Runs())
    scheduler.start_run = lambda run_id: (_ for _ in ()).throw(AssertionError("must not replay wait"))
    await scheduler.recover_incomplete_runs()

