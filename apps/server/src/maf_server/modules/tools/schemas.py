"""Tool、MCP 同步与策略模拟契约。"""

from typing import Any, Literal, NotRequired, TypedDict


class RegisterToolRequest(TypedDict):
    key: str
    name: str
    adapter_type: Literal["NATIVE", "HTTP", "MCP"]
    endpoint_ref: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    approval_mode: Literal["NEVER", "POLICY", "ALWAYS"]
    timeout_seconds: int
    idempotency_key: str


class ToolView(TypedDict):
    id: str
    key: str
    version: int
    name: str
    adapter_type: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    risk_level: str
    approval_mode: str
    status: Literal["ACTIVE", "DISABLED"]


class SyncMcpToolsRequest(TypedDict):
    replace_missing: bool
    idempotency_key: str


class SyncMcpToolsResult(TypedDict):
    created_tool_ids: list[str]
    updated_tool_ids: list[str]
    disabled_tool_ids: list[str]
    warnings: list[str]


class PolicySimulationRequest(TypedDict):
    subject: dict[str, Any]
    action: str
    resource: str
    context: dict[str, Any]


class CapabilityDecisionView(TypedDict):
    allowed: bool
    decision_id: str
    policy_version_id: str
    reason_code: str
    requires_approval: bool
    approval_type: str | None
    constrained_arguments: dict[str, Any] | None
    obligations: list[dict[str, Any]]

