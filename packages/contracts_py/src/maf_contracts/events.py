"""持久化与浏览器 SSE 共用的版本化领域事件。"""

from typing import Any, TypedDict


class ActorRef(TypedDict):
    actor_type: str
    actor_id: str


class DomainEvent(TypedDict):
    event_id: str
    event_type: str
    schema_version: int
    aggregate_type: str
    aggregate_id: str
    organization_id: str
    project_id: str | None
    run_id: str | None
    occurred_at: str
    actor: ActorRef
    trace_id: str
    payload: dict[str, Any]

