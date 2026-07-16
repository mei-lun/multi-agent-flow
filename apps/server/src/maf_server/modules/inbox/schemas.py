"""站内待办、人工决策和通知查询契约。"""

from typing import Any, Literal, TypedDict


class InboxQuery(TypedDict, total=False):
    status: Literal["OPEN", "DECIDED", "EXPIRED", "CANCELLED"]
    item_type: str
    run_id: str
    cursor: str
    limit: int


class InboxItemView(TypedDict):
    id: str
    item_type: str
    title: str
    description: str
    project_id: str
    run_id: str | None
    subject_type: str
    subject_id: str
    subject_version: str
    allowed_decisions: list[str]
    status: Literal["OPEN", "DECIDED", "EXPIRED", "CANCELLED"]
    assigned_user_ids: list[str]
    expires_at: str | None
    created_at: str


class InboxPage(TypedDict):
    items: list[InboxItemView]
    next_cursor: str | None
    has_more: bool


class CreateInboxItem(TypedDict):
    item_type: str
    title: str
    description: str
    project_id: str
    run_id: str | None
    subject_type: str
    subject_id: str
    subject_version: str
    allowed_decisions: list[str]
    assigned_user_ids: list[str]
    expires_at: str | None
    idempotency_key: str


class DecideInboxRequest(TypedDict):
    expected_subject_version: str
    decision: Literal["APPROVE", "REJECT", "REQUEST_CHANGES", "ANSWER"]
    comment: str
    modified_parameters: dict[str, Any] | None
    idempotency_key: str


class InboxDecisionView(TypedDict):
    id: str
    inbox_item_id: str
    decision: str
    comment: str
    modified_parameters: dict[str, Any] | None
    decided_by: str
    decided_at: str
    event_id: str

