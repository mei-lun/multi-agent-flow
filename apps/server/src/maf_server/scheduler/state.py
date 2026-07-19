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
    # References used by graph nodes.  Keep checkpoint payloads small: these are
    # IDs and control fields only; callers may put richer node metadata in
    # ``metadata`` when compiling a graph.
    task_ids: dict[str, str] = field(default_factory=dict)
    gate_decision_ids: list[str] = field(default_factory=list)
    rework_counts: dict[str, int] = field(default_factory=dict)
    waiting_for: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON/checkpoint friendly representation."""
        return {
            "run_id": self.run_id,
            "workflow_version_id": self.workflow_version_id,
            "status": self.status,
            "current_node_ids": list(self.current_node_ids),
            "artifact_ids": list(self.artifact_ids),
            "attempt": self.attempt,
            "task_ids": dict(self.task_ids),
            "gate_decision_ids": list(self.gate_decision_ids),
            "rework_counts": dict(self.rework_counts),
            "waiting_for": self.waiting_for,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_value(cls, value: object) -> "RunState":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise TypeError("scheduler state must be a RunState or mapping")
        known = {f.name for f in __import__("dataclasses").fields(cls)}
        data = {k: v for k, v in value.items() if k in known}
        if not isinstance(data.get("current_node_ids", []), list):
            data["current_node_ids"] = list(data.get("current_node_ids", []))
        if not isinstance(data.get("artifact_ids", []), list):
            data["artifact_ids"] = list(data.get("artifact_ids", []))
        for key in ("task_ids", "rework_counts", "metadata"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        if not isinstance(data.get("gate_decision_ids", []), list):
            data["gate_decision_ids"] = list(data.get("gate_decision_ids", []))
        return cls(**data)  # type: ignore[arg-type]
