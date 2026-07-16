"""Git 协调同步、投影和事件处理结果。"""

from typing import Literal, TypedDict


class SyncResult(TypedDict):
    previous_control_commit: str | None
    current_control_commit: str
    discovered_events: int
    accepted_events: int
    rejected_events: int
    projected_tasks: int


class ProjectorState(TypedDict):
    repository_binding_id: str
    control_branch: str
    projected_control_commit: str | None
    status: Literal["READY", "SYNCING", "ERROR", "REBUILDING"]
    last_error: str | None
    updated_at: str

