"""Serializable scheduler state shared by graph nodes and checkpoints.

Only stable identifiers and small control fields belong here. Large prompts,
documents, logs, and code archives must be stored as artifacts and referenced
by ID.
"""

from dataclasses import dataclass, field


@dataclass(slots=True)
class RunState:
    run_id: str
    workflow_version_id: str
    status: str = "created"
    current_node_ids: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    attempt: int = 0

