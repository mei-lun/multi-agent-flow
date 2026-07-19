"""Run、Task、Attempt 和事件投影持久化接口及内存实现。"""

import asyncio
from copy import deepcopy
from typing import Any, Protocol

from maf_domain.errors import NotFoundError, VersionConflictError
from .schemas import *


class RunRepository(Protocol):
    async def get_run(self, run_id: str) -> RunView | None:
        """读取 Run 当前业务投影；不存在为 None。"""
        ...
    async def save_run(self, run: RunView, expected_version: int | None) -> RunView:
        """按合法状态转换和 expected_version 保存，返回新版本。"""
        ...
    async def get_graph_projection(self, run_id: str) -> RunGraphView | None:
        """读取给 UI 的 graph projection，不访问 checkpoint 数据库。"""
        ...
    async def list_tasks(self, run_id: str, query: TaskQuery) -> TaskPage:
        """按 run/status/node 分页，附带有限 Attempt 摘要。"""
        ...
    async def get_task(self, task_id: str) -> TaskView | None:
        """返回 Task 及 Attempt 历史；不存在为 None。"""
        ...
    async def save_task(self, task: TaskView, expected_version: int | None) -> TaskView:
        """保存合法 Task 状态转换；已终态不能回到 RUNNING。"""
        ...
    async def append_attempt(self, task_id: str, attempt: AttemptView) -> AttemptView:
        """原子分配递增 attempt_no，并保留全部历史 Attempt。"""
        ...
    async def read_events_after(self, run_id: str, event_id: str | None, limit: int) -> list[RunEventView]:
        """按持久顺序读取 event_id 之后的有限事件，供 SSE 回放。"""
        ...


_TERMINAL_RUN_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}


class InMemoryRunRepository:
    """Atomic concrete repository used by services and deterministic replay tests."""

    def __init__(self) -> None:
        self.runs: dict[str, RunView] = {}
        self.snapshots: dict[str, RunSnapshot] = {}
        self.snapshot_ids: dict[str, str] = {}
        self.tasks: dict[str, TaskView] = {}
        self.graphs: dict[str, RunGraphView] = {}
        self.events: dict[str, list[RunEventView]] = {}
        self.commands: dict[tuple[str, str], CommandResult] = {}
        self.idempotency: dict[tuple[str, str], tuple[str, str]] = {}
        self.control_signals: dict[str, list[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def create_run_with_snapshot(
        self,
        run: RunView,
        snapshot: RunSnapshot,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> RunView:
        scope = (run["project_id"], idempotency_key)
        async with self._lock:
            existing = self.idempotency.get(scope)
            if existing is not None:
                if existing[0] != request_hash:
                    from maf_domain.errors import IdempotencyConflictError
                    raise IdempotencyConflictError("run idempotency key payload differs")
                return deepcopy(self.runs[existing[1]])
            self.snapshots[run["snapshot_artifact_version_id"]] = deepcopy(snapshot)
            self.snapshot_ids[run["id"]] = run["snapshot_artifact_version_id"]
            self.runs[run["id"]] = deepcopy(run)
            self.idempotency[scope] = (request_hash, run["id"])
            self.events[run["id"]] = []
        return deepcopy(run)

    async def get_snapshot(self, run_id: str) -> RunSnapshot | None:
        snapshot_id = self.snapshot_ids.get(run_id)
        item = self.snapshots.get(snapshot_id or "")
        return deepcopy(item) if item else None

    async def get_run(self, run_id: str) -> RunView | None:
        item = self.runs.get(run_id)
        return deepcopy(item) if item else None

    async def save_run(self, run: RunView, expected_version: int | None) -> RunView:
        async with self._lock:
            current = self.runs.get(run["id"])
            if current is None:
                raise NotFoundError(f"run not found: {run['id']}")
            if expected_version is not None and current["version"] != expected_version:
                raise VersionConflictError("run version conflict")
            if current["status"] in _TERMINAL_RUN_STATUSES and run["status"] != current["status"]:
                raise VersionConflictError("terminal run is immutable")
            saved = deepcopy(run)
            saved["version"] = current["version"] + 1
            self.runs[run["id"]] = saved
        return deepcopy(saved)

    async def list_incomplete_runs(self) -> list[RunView]:
        return [deepcopy(item) for item in self.runs.values() if item["status"] not in _TERMINAL_RUN_STATUSES]

    async def get_graph_projection(self, run_id: str) -> RunGraphView | None:
        item = self.graphs.get(run_id)
        return deepcopy(item) if item else None

    async def save_graph_projection(self, graph: RunGraphView) -> None:
        self.graphs[graph["run_id"]] = deepcopy(graph)

    async def list_tasks(self, run_id: str, query: TaskQuery) -> TaskPage:
        items = [deepcopy(item) for item in self.tasks.values() if item["run_id"] == run_id]
        if query.get("status"):
            items = [item for item in items if item["status"] == query["status"]]
        if query.get("node_key"):
            items = [item for item in items if item["node_key"] == query["node_key"]]
        items.sort(key=lambda item: item["id"])
        limit = max(1, min(int(query.get("limit", 100)), 200))
        return {"items": items[:limit], "next_cursor": None, "has_more": len(items) > limit}

    async def get_task(self, task_id: str) -> TaskView | None:
        item = self.tasks.get(task_id)
        return deepcopy(item) if item else None

    async def save_task(self, task: TaskView, expected_version: int | None) -> TaskView:
        self.tasks[task["id"]] = deepcopy(task)
        return deepcopy(task)

    async def append_attempt(self, task_id: str, attempt: AttemptView) -> AttemptView:
        task = self.tasks.get(task_id)
        if task is None:
            raise NotFoundError(f"task not found: {task_id}")
        item = deepcopy(attempt)
        item["attempt_no"] = len(task["attempts"]) + 1
        task["attempts"].append(item)
        return deepcopy(item)

    async def append_event(self, event: RunEventView) -> None:
        stream = self.events.setdefault(event["run_id"], [])
        if not any(item["event_id"] == event["event_id"] for item in stream):
            stream.append(deepcopy(event))

    async def read_events_after(self, run_id: str, event_id: str | None, limit: int) -> list[RunEventView]:
        stream = self.events.get(run_id, [])
        offset = 0
        if event_id is not None:
            offset = next((index + 1 for index, item in enumerate(stream) if item["event_id"] == event_id), len(stream))
        return deepcopy(stream[offset:offset + limit])

    async def record_command(self, run_id: str, key: str, result: CommandResult) -> CommandResult:
        existing = self.commands.get((run_id, key))
        if existing is not None:
            return deepcopy(existing)
        self.commands[(run_id, key)] = deepcopy(result)
        return deepcopy(result)

    async def apply_command(
        self, run_id: str, key: str, *, expected_version: int, target_status: str
    ) -> CommandResult:
        """Persist command and state transition under one lock before side effects."""
        async with self._lock:
            existing = self.commands.get((run_id, key))
            if existing is not None:
                return deepcopy(existing)
            run = self.runs.get(run_id)
            if run is None:
                raise NotFoundError(f"run not found: {run_id}")
            if run["version"] != expected_version:
                raise VersionConflictError("run command version conflict")
            if run["status"] in _TERMINAL_RUN_STATUSES:
                raise VersionConflictError("terminal run cannot accept commands")
            updated = deepcopy(run)
            updated["status"] = target_status  # type: ignore[typeddict-item]
            updated["version"] = run["version"] + 1
            self.runs[run_id] = updated
            result: CommandResult = {
                "command_id": key,
                "run_id": run_id,
                "status": "ACCEPTED",
                "run_version": updated["version"],
            }
            self.commands[(run_id, key)] = result
            return deepcopy(result)

    async def get_command(self, run_id: str, key: str) -> CommandResult | None:
        item = self.commands.get((run_id, key))
        return deepcopy(item) if item else None

    async def append_control_signal(self, run_id: str, signal: dict[str, Any]) -> None:
        signals = self.control_signals.setdefault(run_id, [])
        if not any(item.get("command_id") == signal.get("command_id") for item in signals):
            signals.append(deepcopy(signal))


__all__ = ["RunRepository", "InMemoryRunRepository"]
