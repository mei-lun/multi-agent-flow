"""Authenticated HTTP client for Runner registration and job protocol."""


class RunnerApiClient:
    def claim_job(self) -> dict | None:
        raise NotImplementedError

    def heartbeat(self, job_id: str, lease_token: str) -> None:
        raise NotImplementedError

    def complete_job(self, job_id: str, lease_token: str, result: dict) -> None:
        raise NotImplementedError

