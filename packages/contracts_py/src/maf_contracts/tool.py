"""获授权 Tool 调用、人工审批、进度和结果契约。"""

from typing import Any, Literal, TypedDict


class ToolDescriptor(TypedDict):
    key: str
    version: int
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    risk_level: str


class ToolListResponse(TypedDict):
    attempt_id: str
    tools: list[ToolDescriptor]


class ToolCallRequest(TypedDict):
    attempt_id: str
    tool_version: int
    arguments: dict[str, Any]
    call_key: str
    timeout_seconds: int


class ToolCallResult(TypedDict):
    call_id: str
    status: Literal["COMPLETED", "WAITING_APPROVAL", "FAILED", "CANCELLED"]
    output: dict[str, Any] | None
    output_artifact_version_ids: list[str]
    approval_inbox_item_id: str | None
    duration_ms: int
    error: dict[str, Any] | None


class ToolCallView(TypedDict):
    call_id: str
    attempt_id: str
    tool_key: str
    status: str
    created_at: str
    completed_at: str | None
    result: ToolCallResult | None


class CancelToolCallRequest(TypedDict):
    reason: str
    idempotency_key: str

