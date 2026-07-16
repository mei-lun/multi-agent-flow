"""不含密钥的模型调用与统一响应契约。"""

from typing import Any, Literal, NotRequired, TypedDict


class CanonicalMessage(TypedDict):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]]
    name: NotRequired[str]
    tool_call_id: NotRequired[str]


class UnifiedModelRequest(TypedDict):
    attempt_id: str
    call_key: str
    model_policy_id: str
    messages: list[CanonicalMessage]
    tools: list[dict[str, Any]]
    response_schema: dict[str, Any] | None
    temperature: float | None
    max_output_tokens: int
    timeout_seconds: int
    metadata: dict[str, str]


class ModelUsage(TypedDict):
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    estimated_cost: str
    currency: str


class UnifiedModelResponse(TypedDict):
    call_id: str
    status: Literal["COMPLETED", "FAILED", "CANCELLED"]
    model_profile_id: str
    provider_request_id: str | None
    message: CanonicalMessage | None
    tool_calls: list[dict[str, Any]]
    usage: ModelUsage
    latency_ms: int
    finish_reason: str | None
    error: dict[str, Any] | None


class ModelProbeRequest(TypedDict):
    connection_id: str
    model_profile_id: str | None
    checks: list[str]


class ModelCallView(TypedDict):
    call_id: str
    attempt_id: str
    status: str
    selected_model_profile_id: str | None
    usage: ModelUsage | None
    error: dict[str, Any] | None
    created_at: str


class CancelModelCallRequest(TypedDict):
    reason: str
    idempotency_key: str

