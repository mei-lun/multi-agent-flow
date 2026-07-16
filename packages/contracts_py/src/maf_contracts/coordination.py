"""GitHub 跨节点协调使用的机器可读契约。"""

from typing import Any, Literal, NotRequired, TypedDict


TaskStatus = Literal[
    "PLANNED", "READY", "ASSIGNED", "IN_PROGRESS", "BLOCKED", "SUBMITTED",
    "REVIEWING", "REWORK_REQUIRED", "LEASE_EXPIRED", "DONE", "FAILED", "CANCELLED",
]

CoordinationEventType = Literal[
    "NODE_REGISTERED", "NODE_UPDATED", "CLAIM_REQUESTED", "PROGRESS_REPORTED",
    "BLOCKED_REPORTED", "SUBMISSION_CREATED", "WORK_ABANDONED",
]


class NodeManifest(TypedDict):
    schema_version: int
    node_id: str
    display_name: str
    git_identity: dict[str, str]
    capabilities: list[str]
    model_aliases: list[str]
    docker_profiles: list[str]
    capacity: int
    status: Literal["ACTIVE", "DRAINING", "OFFLINE", "QUARANTINED"]
    software_version: str
    version: int


class TaskAssignment(TypedDict):
    node_id: str
    assignment_id: str
    assignment_epoch: int
    assigned_at: str
    expires_at: str
    based_on_control_commit: str


class TaskProgress(TypedDict):
    percent: int
    completed_items: list[str]
    remaining_items: list[str]
    problems: list[dict[str, Any]]
    current_head_commit: str | None
    test_summary: str | None
    last_reported_at: str | None


class TaskDelivery(TypedDict):
    branch: str | None
    base_commit: str | None
    head_commit: str | None
    pull_request_url: str | None
    changed_paths: list[str]
    test_report_path: str | None
    known_issues: list[str]


class CoordinationTask(TypedDict):
    schema_version: int
    task_id: str
    parent_task_id: str | None
    title: str
    description: str
    status: TaskStatus
    priority: int
    requirements: dict[str, Any]
    dependencies: list[str]
    assignment: TaskAssignment | None
    progress: TaskProgress
    delivery: TaskDelivery
    version: int


class CoordinationEvent(TypedDict):
    schema_version: int
    event_id: str
    event_type: CoordinationEventType
    node_id: str
    task_id: str | None
    assignment_id: str | None
    assignment_epoch: int | None
    based_on_control_commit: str
    occurred_at: str
    payload: dict[str, Any]


class EventDecision(TypedDict):
    event_id: str
    accepted: bool
    reason_code: str
    control_commit: str | None
    resulting_task_status: NotRequired[TaskStatus]


class CoordinationSnapshot(TypedDict):
    project_id: str
    control_commit: str
    tasks: list[CoordinationTask]
    nodes: list[NodeManifest]
    generated_at: str

