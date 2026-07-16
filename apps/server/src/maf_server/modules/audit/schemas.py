"""审计查询和写入契约。"""

from typing import Any, TypedDict


class AuditEvent(TypedDict):
    id: str
    actor_type: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: str
    outcome: str
    reason_code: str | None
    trace_id: str
    occurred_at: str
    metadata: dict[str, Any]


class AuditQuery(TypedDict, total=False):
    actor_id: str
    action: str
    resource_type: str
    resource_id: str
    from_time: str
    to_time: str
    cursor: str
    limit: int


class AuditPage(TypedDict):
    items: list[AuditEvent]
    next_cursor: str | None
    has_more: bool

