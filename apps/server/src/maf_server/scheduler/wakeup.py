"""Convert job completions, approvals, and timers into idempotent graph wakeups."""

import inspect
from datetime import datetime, timezone


class WakeupService:
    def __init__(self, scheduler_service: object | None = None, *, repository: object | None = None) -> None:
        self._scheduler = scheduler_service
        self._repository = repository
        self._records: dict[str, dict[str, object]] = {}

    async def wake(
        self, run_id: str, event_id: str, *, task_id: str | None = None,
        assignment_epoch: int | None = None, payload: dict[str, object] | None = None,
    ) -> None:
        """把 Runner、人工或计时事件转换为一次幂等 Scheduler resume。

        先在 scheduler_wakeups 以 event_id 插入去重记录；重复事件直接成功；确认事件所属
        run 与当前等待条件匹配，再调用 SchedulerService.resume_run。失败保留待重试状态。
        """
        if not run_id or not event_id:
            raise ValueError("run_id and event_id are required")
        existing = self._records.get(event_id)
        if existing is None and self._repository is not None:
            lookup = getattr(self._repository, "get_wakeup", None)
            if lookup is not None:
                existing = lookup(event_id)
                if inspect.isawaitable(existing):
                    existing = await existing
        if existing is not None:
            if existing.get("run_id") != run_id:
                raise ValueError("event_id already belongs to another run")
            if existing.get("status") == "processed":
                return
        record = existing or {
            "run_id": run_id,
            "event_id": event_id,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "attempts": 0,
        }
        record["attempts"] = int(record.get("attempts", 0)) + 1
        self._records[event_id] = record
        persist = getattr(self._repository, "record_wakeup", None)
        if persist is not None:
            result = persist(run_id, event_id, "pending")
            if inspect.isawaitable(result):
                await result
        wait_condition = None
        if self._repository is not None:
            getter = getattr(self._repository, "get_wait_condition", None)
            if getter is not None:
                wait_condition = getter(run_id)
                if inspect.isawaitable(wait_condition):
                    wait_condition = await wait_condition
        if isinstance(wait_condition, dict):
            current_task = wait_condition.get("task_id")
            current_epoch = wait_condition.get("assignment_epoch")
            if task_id is not None and current_task not in {None, task_id}:
                record["status"] = "ignored"
                record["error"] = "event task is not the current waiting task"
                return
            if assignment_epoch is not None and current_epoch not in {None, assignment_epoch}:
                record["status"] = "ignored"
                record["error"] = "event assignment epoch is stale"
                return
        if self._scheduler is None:
            record["status"] = "processed"
            return
        try:
            validate = getattr(self._scheduler, "accept_wakeup", None)
            if validate is not None:
                result = validate(run_id, event_id)
                if inspect.isawaitable(result):
                    result = await result
                if result is False:
                    record["status"] = "ignored"
                    return
            resume = getattr(self._scheduler, "resume_run")
            command = {"event_id": event_id, "command_id": event_id, "reason": "wakeup",
                       "task_id": task_id, "assignment_epoch": assignment_epoch,
                       "payload": payload or {}}
            result = resume(run_id, command)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = str(exc)
            persist = getattr(self._repository, "record_wakeup", None)
            if persist is not None:
                result = persist(run_id, event_id, "failed", str(exc))
                if inspect.isawaitable(result):
                    await result
            raise
        record["status"] = "processed"
        persist = getattr(self._repository, "record_wakeup", None)
        if persist is not None:
            result = persist(run_id, event_id, "processed")
            if inspect.isawaitable(result):
                await result
