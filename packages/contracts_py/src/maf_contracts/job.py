"""节点本地执行 Attempt 使用的不可变任务和结果契约。"""

from typing import Any, Literal, TypedDict


class AttemptBudget(TypedDict):
    max_input_tokens: int
    max_output_tokens: int
    max_cost_amount: str
    currency: str


class WorkspaceSpec(TypedDict):
    kind: Literal["GENERIC", "GIT"]
    repository_path: str
    work_branch: str
    base_commit: str
    writable_subpaths: list[str]


class TaskDispatchEnvelope(TypedDict):
    project_id: str
    task_id: str
    assignment_id: str
    assignment_epoch: int
    based_on_control_commit: str
    task_type: str
    role_version_ref: dict[str, Any] | None
    input_refs: list[str]
    output_contract: dict[str, Any]
    resource_profile: str
    docker_image_digest: str
    workspace: WorkspaceSpec
    network_policy_ref: dict[str, Any]
    capability_policy_ref: dict[str, Any]
    timeout_seconds: int
    max_steps: int
    max_tool_calls: int
    budget: AttemptBudget


class WorkspaceResult(TypedDict):
    branch: str
    base_commit: str
    head_commit: str
    tree_hash: str
    changed_paths: list[str]


class AttemptResult(TypedDict):
    task_id: str
    assignment_id: str
    assignment_epoch: int
    status: Literal["SUBMITTED", "BLOCKED", "FAILED", "CANCELLED"]
    output_paths: list[str]
    execution_summary: str
    self_check: list[dict[str, Any]]
    known_risks: list[dict[str, Any]]
    remaining_items: list[str]
    model_usage: dict[str, Any]
    tool_usage: dict[str, Any]
    workspace_result: WorkspaceResult | None
    error: dict[str, Any] | None


class InfrastructureFailure(TypedDict):
    code: str
    message: str
    retryable: bool
    phase: str
    diagnostics_path: str | None

