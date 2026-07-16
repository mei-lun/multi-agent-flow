"""Review 查询和 Quality Gate 契约。"""

from typing import Any, Literal, TypedDict


class ReviewQuery(TypedDict, total=False):
    project_id: str
    run_id: str
    review_type: str
    status: str
    cursor: str
    limit: int


class ReviewView(TypedDict):
    id: str
    run_id: str
    task_id: str | None
    review_type: Literal["ARCHITECTURE", "CODE", "TEST", "ACCEPTANCE", "HUMAN"]
    reviewer_role_version_id: str | None
    status: Literal["PENDING", "PASS", "FAIL", "CHANGES_REQUESTED"]
    blocking_items: list[dict[str, Any]]
    warning_items: list[dict[str, Any]]
    evidence_artifact_version_ids: list[str]
    completed_at: str | None


class ReviewPage(TypedDict):
    items: list[ReviewView]
    next_cursor: str | None
    has_more: bool


class GateDefinition(TypedDict):
    node_key: str
    required_review_types: list[str]
    required_artifact_contracts: list[dict[str, Any]]
    blocking_severities: list[str]
    allow_human_override: bool
    rework_target_by_category: dict[str, str]


class GateInputs(TypedDict):
    run_id: str
    source_node_key: str
    review_ids: list[str]
    artifact_version_ids: list[str]


class GateDecisionView(TypedDict):
    gate_node_key: str
    decision: Literal["PASS", "REWORK", "WAITING_HUMAN", "FAIL"]
    review_ids: list[str]
    blocking_items: list[dict[str, Any]]
    warning_items: list[dict[str, Any]]
    rework_category: str | None
    target_node_key: str | None
    affected_node_keys: list[str]
    evidence_refs: list[str]

