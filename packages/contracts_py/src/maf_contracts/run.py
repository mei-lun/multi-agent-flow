"""Scheduler 使用的小型 Run Snapshot 与编译图契约。"""

from typing import Any, TypedDict

from .job import AttemptBudget


class RunSnapshotRef(TypedDict):
    run_id: str
    snapshot_artifact_version_id: str
    snapshot_hash: str
    workflow_version_id: str
    project_input_version_id: str
    repository_binding_id: str | None
    start_node_key: str
    limits: dict[str, Any]


class CompactNode(TypedDict):
    key: str
    kind: str
    role_version_id: str | None
    input_contracts: list[dict[str, Any]]
    output_contracts: list[dict[str, Any]]
    retry_policy: dict[str, Any]
    timeout_seconds: int


class CompactEdge(TypedDict):
    key: str
    source_node_key: str
    target_node_key: str
    condition: str | None
    priority: int


class CompactRunGraph(TypedDict):
    workflow_hash: str
    nodes: dict[str, CompactNode]
    outgoing_edges: dict[str, list[CompactEdge]]
    incoming_nodes: dict[str, list[str]]
    success_end_nodes: set[str]
    failure_end_nodes: set[str]

