"""Role Definition、不可变版本、试运行和发布契约。"""

from typing import Any, Literal, TypedDict


class CreateRoleRequest(TypedDict):
    key: str
    name: str
    description: str
    idempotency_key: str


class RoleView(TypedDict):
    id: str
    key: str
    name: str
    description: str
    latest_published_version_id: str | None
    version: int


class CreateRoleVersionRequest(TypedDict):
    system_prompt: str
    model_policy_id: str
    skill_version_ids: list[str]
    tool_grants: list[dict[str, Any]]
    capability_policy_version_id: str
    resource_profile: str
    network_policy_version_id: str
    max_steps: int
    max_tool_calls: int
    timeout_seconds: int
    change_summary: str
    idempotency_key: str


class RoleVersionView(TypedDict):
    id: str
    role_id: str
    version: int
    status: Literal["DRAFT", "PUBLISHED", "RETIRED"]
    system_prompt_hash: str
    model_policy_id: str
    skill_version_ids: list[str]
    tool_grants: list[dict[str, Any]]
    capability_policy_version_id: str
    resource_profile: str
    network_policy_version_id: str
    limits: dict[str, int]
    content_hash: str


class DryRunRoleRequest(TypedDict):
    input_artifact_version_ids: list[str]
    instruction: str
    max_cost: str
    idempotency_key: str


class DryRunRoleResult(TypedDict):
    run_id: str
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED"]
    output_artifact_version_ids: list[str]
    validation_report: dict[str, Any]


class PublishRoleRequest(TypedDict):
    expected_version: int
    idempotency_key: str


class ValidationReport(TypedDict):
    valid: bool
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]

