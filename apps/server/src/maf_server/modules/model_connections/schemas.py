"""模型连接、模型配置、策略和用量查询契约。"""

from typing import Any, Literal, NotRequired, TypedDict


class CreateModelConnectionRequest(TypedDict):
    name: str
    adapter_type: Literal[
        "OPENAI_COMPAT_CHAT", "CODEX", "GLM", "DEEPSEEK", "MINIMAX", "KIMI_CODE"
    ]
    base_url: str
    api_key: str
    request_timeout_ms: int
    tls_verify: bool
    idempotency_key: str


class ModelConnectionView(TypedDict):
    id: str
    name: str
    adapter_type: str
    base_url: str
    request_timeout_ms: int
    tls_verify: bool
    api_key_configured: bool
    secret_fingerprint_suffix: str | None
    status: Literal["UNVERIFIED", "READY", "ERROR", "DISABLED"]
    version: int


class ConnectionQuery(TypedDict, total=False):
    cursor: str
    limit: int
    status: str
    adapter_type: str


class ConnectionPage(TypedDict):
    items: list[ModelConnectionView]
    next_cursor: str | None
    has_more: bool


class VerifyConnectionRequest(TypedDict):
    levels: list[Literal["DNS", "TLS", "AUTH", "CHAT"]]
    idempotency_key: str


class ProbeCheck(TypedDict):
    name: str
    status: Literal["PASS", "FAIL", "SKIP"]
    latency_ms: int | None
    message: str


class ProbeResult(TypedDict):
    status: Literal["PASS", "PARTIAL", "FAIL"]
    checks: list[ProbeCheck]
    checked_at: str


class RegisterModelRequest(TypedDict):
    remote_model_name: str
    display_name: str
    context_window: int | None
    input_price_per_million: str | None
    output_price_per_million: str | None
    idempotency_key: str


class ModelProfileView(TypedDict):
    id: str
    connection_id: str
    remote_model_name: str
    display_name: str
    capabilities: dict[str, bool | int | str | None]
    status: Literal["UNPROBED", "READY", "LIMITED", "ERROR"]
    version: int


class ProbeModelRequest(TypedDict):
    capabilities: list[Literal["CHAT", "STREAM", "TOOLS", "JSON_SCHEMA", "VISION"]]
    idempotency_key: str


class CreateModelPolicyRequest(TypedDict):
    name: str
    primary_model_profile_id: str
    fallback_model_profile_ids: list[str]
    max_retries_per_model: int
    allow_fallback: bool
    temperature: NotRequired[float]
    max_output_tokens: NotRequired[int]
    idempotency_key: str


class ModelPolicyView(TypedDict):
    id: str
    name: str
    primary_model_profile_id: str
    fallback_model_profile_ids: list[str]
    max_retries_per_model: int
    allow_fallback: bool
    version: int


class UsageQuery(TypedDict, total=False):
    project_id: str
    run_id: str
    role_version_id: str
    model_profile_id: str
    from_time: str
    to_time: str
    cursor: str
    limit: int


class ModelUsageItem(TypedDict):
    model_profile_id: str
    run_id: str | None
    input_tokens: int
    output_tokens: int
    estimated_cost: str
    currency: str
    latency_ms: int
    status: str
    occurred_at: str


class UsagePage(TypedDict):
    items: list[ModelUsageItem]
    next_cursor: str | None
    has_more: bool
    totals: dict[str, Any]

