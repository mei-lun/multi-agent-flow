"""Convert job completions, approvals, and timers into idempotent graph wakeups."""


class WakeupService:
    async def wake(self, run_id: str, event_id: str) -> None:
        """把 Runner、人工或计时事件转换为一次幂等 Scheduler resume。

        先在 scheduler_wakeups 以 event_id 插入去重记录；重复事件直接成功；确认事件所属
        run 与当前等待条件匹配，再调用 SchedulerService.resume_run。失败保留待重试状态。
        """
        ...
