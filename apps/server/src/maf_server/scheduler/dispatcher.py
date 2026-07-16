"""Turn ready graph nodes into durable Runner jobs."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    run_id: str
    node_run_id: str
    role_snapshot_id: str
    workspace_kind: str


class JobDispatcher:
    """Persists dispatch requests; it never calls a Runner directly."""

    def dispatch(self, request: DispatchRequest) -> str:
        raise NotImplementedError

