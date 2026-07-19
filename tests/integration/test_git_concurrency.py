"""TASK-096 deterministic Git coordination concurrency contract tests."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from maf_server.modules.git_coordination.service import TaskAllocator


def _task(task_id: str = "TASK-001") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": "READY",
        "priority": 10,
        "requirements": {"required_capabilities": ["python"]},
        "dependencies": [],
        "assignment": None,
    }


def _node(node_id: str) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "status": "ACTIVE",
        "capacity": 1,
        "capabilities": ["python"],
    }


def _snapshot() -> dict[str, Any]:
    return {"project_id": "p", "control_commit": "a" * 40, "tasks": [_task()]}


@pytest.mark.asyncio
async def test_two_nodes_claiming_same_control_snapshot_have_one_owner() -> None:
    allocator = TaskAllocator()
    snapshot = _snapshot()
    results = await asyncio.gather(
        asyncio.to_thread(allocator.choose_claim, snapshot, "node-a", _node("node-a")),
        asyncio.to_thread(allocator.choose_claim, snapshot, "node-b", _node("node-b")),
    )
    accepted = [result for result in results if result["accepted"]]
    rejected = [result for result in results if not result["accepted"]]
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert rejected[0]["reason_code"] == "no_matching_tasks"
    assert accepted[0]["task_id"] == "TASK-001"


def test_stale_epoch_rejection_uses_stable_reason_code() -> None:
    from maf_server.modules.git_coordination.service import check_assignment_epoch

    result = check_assignment_epoch("TASK-001", event_epoch=1, current_epoch=2)
    assert result.passed is False
    assert result.reason_code == "EVENT_EPOCH_STALE"


def test_invalid_claim_input_is_not_silently_accepted() -> None:
    allocator = TaskAllocator()
    result = allocator.choose_claim(_snapshot(), "node-a", _node("node-b"))
    assert result["accepted"] is False
    assert result["reason_code"] == "node_unavailable"
