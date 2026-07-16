"""Convert job completions, approvals, and timers into idempotent graph wakeups."""


class WakeupService:
    def wake(self, run_id: str, event_id: str) -> None:
        raise NotImplementedError

