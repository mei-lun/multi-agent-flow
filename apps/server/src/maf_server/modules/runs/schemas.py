"""Run、Task、Attempt、命令、图投影和事件查询契约。"""

from typing import Any, Literal, NotRequired, TypedDict


class RunLimits(TypedDict):
    budget_amount: str
    currency: str
    token_budget: int
    max_tasks: int
    max_reworks: int
    max_run_seconds: int


class StartRunRequest(TypedDict):
    workflow_version_id: str
    project_input_version_id: str
    repository_binding_id: str | None
    limits: RunLimits
    idempotency_key: str


class RunView(TypedDict):
    id: str
    project_id: str
    workflow_version_id: str
    snapshot_artifact_version_id: str
    status: Literal["CREATED", "RUNNING", "PAUSED", "WAITING_HUMAN", "COMPLETED", "FAILED", "CANCELLING", "CANCELLED"]
    limits: RunLimits
    consumed: dict[str, int | str]
    started_at: str | None
    completed_at: str | None
    failure_code: str | None
    version: int


class RunNodeView(TypedDict):
    node_key: str
    kind: str
    status: str
    latest_task_id: str | None
    attempt_count: int
    output_artifact_version_ids: list[str]


class RunGraphView(TypedDict):
    run_id: str
    nodes: list[RunNodeView]
    edges: list[dict[str, Any]]
    current_node_keys: list[str]
    projection_version: int


class AttemptView(TypedDict):
    id: str
    task_id: str
    attempt_no: int
    status: str
    runner_id: str | None
    started_at: str | None
    completed_at: str | None
    error: dict[str, Any] | None


class TaskView(TypedDict):
    id: str
    run_id: str
    node_key: str
    status: str
    attempts: list[AttemptView]


class TaskPage(TypedDict):
    items: list[TaskView]
    next_cursor: str | None
    has_more: bool


class RunEventView(TypedDict):
    event_id: str
    event_type: str
    occurred_at: str
    run_id: str
    payload: dict[str, Any]


class RunCommand(TypedDict):
    reason: str
    expected_version: int
    idempotency_key: str


class ResumeRunRequest(RunCommand, total=False):
    human_decision_id: str


class IncreaseBudgetRequest(TypedDict):
    additional_amount: str
    currency: str
    additional_tokens: int
    reason: str
    expected_version: int
    idempotency_key: str


class RetryTaskRequest(TypedDict):
    reason: str
    reset_to_artifact_version_ids: list[str]
    expected_task_version: int
    idempotency_key: str


class CommandResult(TypedDict):
    command_id: str
    run_id: str
    status: Literal["ACCEPTED", "APPLIED", "REJECTED"]
    run_version: int


class RunSnapshot(TypedDict):
    project: dict[str, Any]
    project_input: dict[str, Any]
    repository_binding: dict[str, Any] | None
    workflow_version: dict[str, Any]
    workflow_graph: dict[str, Any]
    role_versions: list[dict[str, Any]]
    skill_versions: list[dict[str, Any]]
    tool_versions: list[dict[str, Any]]
    model_policies: list[dict[str, Any]]
    control_base_commit: str
    limits: RunLimits
    created_by: str
    created_at: str


class TaskQuery(TypedDict, total=False):
    cursor: str
    limit: int
    status: str
    node_key: str
