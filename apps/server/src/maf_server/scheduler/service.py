"""Application service for starting, resuming, pausing, and cancelling runs."""


class SchedulerService:
    def start_run(self, run_id: str) -> None:
        raise NotImplementedError

    def resume_run(self, run_id: str, event_id: str) -> None:
        raise NotImplementedError

    def cancel_run(self, run_id: str, reason: str) -> None:
        raise NotImplementedError

