"""Git authority based Claim workflow for a Runner node."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Any


class ClaimWorkflow:
    """Select only control-published compatible tasks and wait for acceptance."""

    def __init__(self, *, node_id: str, capacity: int, registry, git_client) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.node_id = node_id
        self.capacity = capacity
        self.registry = registry
        self.git = git_client
        self._active: set[str] = set()
        self._lock = asyncio.Lock()

    def candidates(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        result = []
        for task in snapshot.get("tasks", []) or []:
            if str(task.get("status")) != "READY" or task.get("assignment") is not None:
                continue
            required = list((task.get("requirements") or {}).get("capability_aliases", []))
            if self.registry.can_claim(required):
                result.append(task)
        return result

    async def claim(self, task: dict[str, Any], *, control_commit: str) -> dict[str, Any] | None:
        task_id = str(task.get("task_id", ""))
        async with self._lock:
            if len(self._active) >= self.capacity or not task_id or task_id in self._active:
                return None
            self._active.add(task_id)
        event_id = "evt-claim-" + hashlib.sha256(f"{self.node_id}:{task_id}:{control_commit}".encode()).hexdigest()[:32]
        event = {
            "schema_version": 1, "event_id": event_id, "event_type": "CLAIM_REQUESTED",
            "node_id": self.node_id, "task_id": task_id, "assignment_id": None,
            "assignment_epoch": None, "based_on_control_commit": control_commit,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "payload": {"capability_aliases": (task.get("requirements") or {}).get("capability_aliases", [])},
        }
        try:
            pushed = await self.git.append_event(event)
            if not pushed.get("push_ok", False):
                return None
            accepted = await self.git.wait_for_assignment(task_id, event_id, 60)
            if not accepted or (accepted.get("assignment") or {}).get("node_id") != self.node_id:
                return None
            return accepted
        finally:
            async with self._lock:
                self._active.discard(task_id)


__all__ = ["ClaimWorkflow"]
