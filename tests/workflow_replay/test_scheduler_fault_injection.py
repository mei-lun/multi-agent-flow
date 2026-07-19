"""TASK-097 deterministic scheduler replay and fault-injection scenarios."""

from __future__ import annotations

from copy import deepcopy

import pytest

from maf_server.scheduler.wakeup import WakeupService


class _Scheduler:
    def __init__(self) -> None:
        self.resumes: list[tuple[str, dict]] = []
        self.fail_once = False

    async def resume_run(self, run_id: str, command: dict) -> None:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("injected database failure")
        self.resumes.append((run_id, command))


@pytest.mark.asyncio
async def test_replaying_same_history_is_deterministic_and_idempotent() -> None:
    first = _Scheduler()
    wakeup = WakeupService(first)
    history = [
        ("run-1", "event-task-submitted", "TASK-001", 1),
        ("run-1", "event-human-approved", None, None),
        ("run-1", "event-task-submitted", "TASK-001", 1),
    ]
    for run_id, event_id, task_id, epoch in history:
        await wakeup.wake(run_id, event_id, task_id=task_id, assignment_epoch=epoch)
    replay = _Scheduler()
    replay_wakeup = WakeupService(replay)
    for run_id, event_id, task_id, epoch in history:
        await replay_wakeup.wake(run_id, event_id, task_id=task_id, assignment_epoch=epoch)
    assert first.resumes == replay.resumes
    # Replaying an identical event_id is an idempotent no-op.
    assert len(first.resumes) == 2


@pytest.mark.asyncio
async def test_push_success_then_projection_failure_is_retryable_and_not_duplicated() -> None:
    scheduler = _Scheduler()
    scheduler.fail_once = True
    wakeup = WakeupService(scheduler)
    with pytest.raises(RuntimeError, match="injected"):
        await wakeup.wake("run-1", "event-1", task_id="TASK-001", assignment_epoch=1)
    # Same event is retried after the transient projection/database failure;
    # the event id remains the deduplication key and only one resume succeeds.
    await wakeup.wake("run-1", "event-1", task_id="TASK-001", assignment_epoch=1)
    await wakeup.wake("run-1", "event-1", task_id="TASK-001", assignment_epoch=1)
    assert len(scheduler.resumes) == 1


@pytest.mark.asyncio
async def test_different_event_ids_create_distinct_attempt_wakeups() -> None:
    scheduler = _Scheduler()
    wakeup = WakeupService(scheduler)
    await wakeup.wake("run-1", "event-1", task_id="TASK-001", assignment_epoch=1)
    await wakeup.wake("run-1", "event-2", task_id="TASK-001", assignment_epoch=1)
    assert [item[1]["event_id"] for item in scheduler.resumes] == ["event-1", "event-2"]
