"""Workflow Definition、Graph、校验、发布和差异契约。"""

from typing import Any, Literal, NotRequired, TypedDict


class CreateWorkflowRequest(TypedDict):
    key: str
    name: str
    description: str
    idempotency_key: str


class WorkflowView(TypedDict):
    id: str
    key: str
    name: str
    description: str
    latest_published_version_id: str | None
    version: int


class CreateWorkflowVersionRequest(TypedDict):
    based_on_version_id: str | None
    change_summary: str
    idempotency_key: str


class WorkflowVersionView(TypedDict):
    id: str
    workflow_id: str
    version: int
    status: Literal["DRAFT", "PUBLISHED", "RETIRED"]
    graph_hash: str | None
    validation_status: Literal["NOT_RUN", "PASS", "FAIL"]
    content_hash: str | None
    revision: NotRequired[int]
    change_summary: NotRequired[str]


class WorkflowNode(TypedDict):
    key: str
    kind: Literal["AGENT", "GATE", "HUMAN_GATE", "END_SUCCESS", "END_FAILURE"]
    role_version_id: NotRequired[str]
    input_contracts: list[dict[str, Any]]
    output_contracts: list[dict[str, Any]]
    retry_policy: dict[str, Any]
    timeout_seconds: int
    ui_position: dict[str, float]


class WorkflowEdge(TypedDict):
    key: str
    source_node_key: str
    target_node_key: str
    condition: str | None
    priority: int


class WorkflowGraph(TypedDict):
    start_node_key: str
    nodes: list[WorkflowNode]
    edges: list[WorkflowEdge]


class SaveGraphRequest(TypedDict):
    graph: WorkflowGraph
    expected_version: int
    idempotency_key: str


class ValidationReport(TypedDict):
    valid: bool
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    reachable_node_keys: list[str]


class PublishWorkflowRequest(TypedDict):
    expected_version: int
    idempotency_key: str


class WorkflowDiff(TypedDict):
    base_version_id: str
    other_version_id: str
    added_nodes: list[str]
    removed_nodes: list[str]
    changed_nodes: list[dict[str, Any]]
    added_edges: list[str]
    removed_edges: list[str]
    changed_edges: NotRequired[list[dict[str, Any]]]
