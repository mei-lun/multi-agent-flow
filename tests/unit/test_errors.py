"""TASK-005 单元测试：统一领域错误。

验收标准：
1. 参数、权限、版本、Git 冲突、外部依赖错误可区分。
2. API 不返回堆栈，Git EventDecision 返回稳定 reason_code。
3. 错误类型有单元测试。

测试范围：
- ``packages/domain/src/maf_domain/errors.py``：``ErrorCode``、``ReasonCode``、
  ``DomainError`` 及其子类。
- ``apps/server/src/maf_server/api/errors.py``：``ErrorResponse``、HTTP 状态映射、
  FastAPI 异常处理器。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from maf_domain.errors import (
    AlreadyExistsError,
    ArgumentError,
    BudgetExceededError,
    DomainError,
    ErrorCode,
    ExternalDependencyError,
    GitConflictError,
    GitEventRejectedError,
    IdempotencyConflictError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    ReasonCode,
    ToolPolicyDeniedError,
    UnauthenticatedError,
    UnsupportedOperationError,
    ValidationError,
    VersionConflictError,
)
from maf_server.api.errors import (
    ErrorResponse,
    domain_error_handler,
    http_status_for,
    register_error_handlers,
    to_error_response,
)


# --------------------------------------------------------------------------- #
# 验收标准 1：参数、权限、版本、Git 冲突、外部依赖错误可区分
# --------------------------------------------------------------------------- #


def test_error_code_values_are_unique() -> None:
    """所有 ErrorCode 取值互不相同，便于日志和客户端区分。"""
    values = [member.value for member in ErrorCode]
    assert len(values) == len(set(values))


def test_reason_code_values_are_unique() -> None:
    """所有 ReasonCode 取值互不相同。"""
    values = [member.value for member in ReasonCode]
    assert len(values) == len(set(values))


@pytest.mark.parametrize(
    "exc_cls,expected_code",
    [
        (ArgumentError, ErrorCode.ARGUMENT_INVALID),
        (ValidationError, ErrorCode.SCHEMA_VALIDATION_FAILED),
        (UnsupportedOperationError, ErrorCode.UNSUPPORTED_OPERATION),
        (UnauthenticatedError, ErrorCode.UNAUTHENTICATED),
        (PermissionDeniedError, ErrorCode.PERMISSION_DENIED),
        (ToolPolicyDeniedError, ErrorCode.TOOL_POLICY_DENIED),
        (NotFoundError, ErrorCode.NOT_FOUND),
        (AlreadyExistsError, ErrorCode.ALREADY_EXISTS),
        (VersionConflictError, ErrorCode.VERSION_CONFLICT),
        (IdempotencyConflictError, ErrorCode.IDEMPOTENCY_CONFLICT),
        (GitConflictError, ErrorCode.GIT_CONFLICT),
        (GitEventRejectedError, ErrorCode.GIT_EVENT_REJECTED),
        (RateLimitedError, ErrorCode.RATE_LIMITED),
        (BudgetExceededError, ErrorCode.BUDGET_EXCEEDED),
        (ExternalDependencyError, ErrorCode.EXTERNAL_DEPENDENCY_FAILED),
    ],
)
def test_each_error_subclass_has_distinct_error_code(
    exc_cls: type[DomainError], expected_code: ErrorCode
) -> None:
    """每种错误子类对应一个独立 ErrorCode，可被调用方区分。"""
    if exc_cls is GitEventRejectedError:
        exc = exc_cls("x", reason_code=ReasonCode.EVENT_EPOCH_STALE)
    else:
        exc = exc_cls("x")
    assert exc.error_code == expected_code


def test_parameter_permission_version_git_external_are_distinguishable() -> None:
    """验收：参数、权限、版本、Git 冲突、外部依赖错误 error_code 各不相同。"""
    errors = [
        ArgumentError("参数错误"),
        PermissionDeniedError("无权限"),
        VersionConflictError("版本冲突"),
        GitConflictError("control push 冲突"),
        ExternalDependencyError("模型供应商 503"),
    ]
    codes = [e.error_code for e in errors]
    assert len(codes) == len(set(codes)), "五类错误 error_code 必须互不相同"


# --------------------------------------------------------------------------- #
# 验收标准 2：API 不返回堆栈，Git EventDecision 返回稳定 reason_code
# --------------------------------------------------------------------------- #


def test_git_event_rejected_carries_stable_reason_code() -> None:
    """GitEventRejectedError 必须携带 ReasonCode 枚举值。"""
    exc = GitEventRejectedError(
        "旧 epoch 提交",
        reason_code=ReasonCode.EVENT_EPOCH_STALE,
        context={"task_id": "t-1", "assignment_epoch": 3},
    )
    assert exc.reason_code == ReasonCode.EVENT_EPOCH_STALE.value
    assert exc.reason_code == "EVENT_EPOCH_STALE"


def test_git_event_rejected_rejects_missing_reason_code() -> None:
    """GitEventRejectedError 必须显式提供 reason_code，禁止 None。"""
    with pytest.raises(ValueError):
        GitEventRejectedError("x", reason_code=None)  # type: ignore[arg-type]


def test_reason_code_enum_values_match_names() -> None:
    """ReasonCode 取值与成员名一致，保证跨版本稳定。"""
    for member in ReasonCode:
        assert member.value == member.name


def test_to_error_response_does_not_leak_stack() -> None:
    """to_error_response 序列化结果不包含堆栈、文件路径或 traceback 字段。"""
    try:
        raise GitEventRejectedError(
            "epoch stale",
            reason_code=ReasonCode.EVENT_EPOCH_STALE,
            context={"task_id": "t-1"},
        )
    except GitEventRejectedError as exc:
        response = to_error_response(exc, trace_id="trace-1")

    payload_str = str(response.model_dump())
    forbidden = ("stack", "traceback", "__traceback__", "tb_frame", "lineno", "filename")
    for token in forbidden:
        assert token not in payload_str, f"ErrorResponse 泄露内部信息: {token}"
    assert response.error_code == ErrorCode.GIT_EVENT_REJECTED.value
    assert response.reason_code == "EVENT_EPOCH_STALE"
    assert response.trace_id == "trace-1"


def test_domain_error_to_dict_excludes_stack() -> None:
    """DomainError.to_dict 不包含堆栈字段。"""
    exc = ArgumentError("缺少必填字段")
    data = exc.to_dict()
    assert data["error_code"] == "ARGUMENT_INVALID"
    lowered = str(data).lower()
    assert "stack" not in lowered
    assert "traceback" not in lowered


def test_fastapi_handler_returns_json_without_stack() -> None:
    """FastAPI 异常处理器返回 JSON 响应，正文只含稳定字段，不含堆栈。"""
    app = FastAPI()

    @app.get("/raise")
    async def _raise() -> None:
        raise GitEventRejectedError(
            "stale epoch",
            reason_code=ReasonCode.EVENT_EPOCH_STALE,
            context={"assignment_epoch": 5},
        )

    register_error_handlers(app)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/raise", headers={"traceparent": "00-trace123-span1-01"})
    assert resp.status_code == 409
    body = resp.json()
    assert "error" in body
    err = body["error"]
    assert err["error_code"] == "GIT_EVENT_REJECTED"
    assert err["reason_code"] == "EVENT_EPOCH_STALE"
    assert err["trace_id"] == "trace123"
    body_str = str(body).lower()
    for token in ("stack", "traceback", "lineno", "filename"):
        assert token not in body_str


# --------------------------------------------------------------------------- #
# 验收标准 3：错误类型有单元测试（HTTP 映射 + 序列化 + 行为）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "exc,expected_status",
    [
        (ArgumentError("bad"), 400),
        (UnsupportedOperationError("nope"), 400),
        (UnauthenticatedError("no auth"), 401),
        (PermissionDeniedError("forbidden"), 403),
        (ToolPolicyDeniedError("tool denied", reason_code=ReasonCode.POLICY_CASBIN_DENIED), 403),
        (NotFoundError("missing"), 404),
        (VersionConflictError("v"), 409),
        (IdempotencyConflictError("idem"), 409),
        (GitConflictError("push"), 409),
        (
            GitEventRejectedError("rejected", reason_code=ReasonCode.EVENT_DUPLICATE),
            409,
        ),
        (ValidationError("schema"), 422),
        (RateLimitedError("rl"), 429),
        (BudgetExceededError("budget"), 429),
        (ExternalDependencyError("dep"), 503),
    ],
)
def test_http_status_mapping(exc: DomainError, expected_status: int) -> None:
    """http_status_for 按 ErrorCode 映射到正确 HTTP 状态码。"""
    assert http_status_for(exc) == expected_status


def test_error_response_serializes_to_json_compatible_dict() -> None:
    """ErrorResponse 可被 pydantic 序列化为 JSON 兼容 dict。"""
    exc = ExternalDependencyError(
        "模型供应商不可用",
        context={"provider": "litellm", "status_code": 503},
        retryable=True,
    )
    response = to_error_response(exc, trace_id="trace-abc")
    data = response.model_dump(exclude_none=True)
    assert data["error_code"] == "EXTERNAL_DEPENDENCY_FAILED"
    assert data["retryable"] is True
    assert data["details"] == {"provider": "litellm", "status_code": 503}
    assert data["trace_id"] == "trace-abc"
    assert "reason_code" not in data  # None 值被 exclude_none 剔除


def test_error_response_pydantic_model_validates() -> None:
    """ErrorResponse 是 pydantic 模型，可正常实例化。"""
    resp = ErrorResponse(
        error_code="ARGUMENT_INVALID",
        message="missing field",
        retryable=False,
    )
    assert resp.error_code == "ARGUMENT_INVALID"
    assert resp.retryable is False
    assert resp.reason_code is None
    assert resp.details is None
    assert resp.trace_id is None


def test_rate_limited_handler_adds_retry_after_header() -> None:
    """RateLimitedError 携带 retry_after 时，响应头包含 Retry-After。"""
    app = FastAPI()

    @app.get("/raise")
    async def _raise() -> None:
        raise RateLimitedError("too many", context={"retry_after": 30})

    register_error_handlers(app)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/raise")
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "30"


def test_context_is_copied_not_shared() -> None:
    """DomainError.context 应复制输入 dict，避免外部修改影响错误实例。"""
    ctx = {"task_id": "t-1"}
    exc = NotFoundError("missing", context=ctx)
    ctx["task_id"] = "t-2"
    assert exc.context["task_id"] == "t-1"


def test_retryable_defaults_per_subclass() -> None:
    """RateLimitedError/ExternalDependencyError 默认可重试，其他默认不重试。"""
    assert RateLimitedError("x").retryable is True
    assert ExternalDependencyError("x").retryable is True
    assert ArgumentError("x").retryable is False
    assert VersionConflictError("x").retryable is False
    assert GitConflictError("x").retryable is False


def test_retryable_override_via_constructor() -> None:
    """构造时可显式覆盖 retryable 默认值。"""
    exc = ExternalDependencyError("x", retryable=False)
    assert exc.retryable is False
    exc2 = ArgumentError("x", retryable=True)
    assert exc2.retryable is True


def test_unsupported_operation_error_covers_unsupported_node_type_case() -> None:
    """对应系统设计 25.9 ``DomainError("UNSUPPORTED_NODE_TYPE")`` 用例。"""
    exc = UnsupportedOperationError(
        "unsupported node type",
        context={"node_type": "UNKNOWN"},
    )
    assert exc.error_code == ErrorCode.UNSUPPORTED_OPERATION
    assert exc.context == {"node_type": "UNKNOWN"}
    assert http_status_for(exc) == 400


def test_tool_policy_denied_carries_policy_reason_code() -> None:
    """ToolPolicyDeniedError 携带 ``POLICY_*`` reason_code（对应系统设计 25.13）。"""
    exc = ToolPolicyDeniedError(
        "casbin denied",
        reason_code=ReasonCode.POLICY_CASBIN_DENIED,
        context={"tool_key": "filesystem.write"},
    )
    assert exc.error_code == ErrorCode.TOOL_POLICY_DENIED
    assert exc.reason_code == "POLICY_CASBIN_DENIED"
    assert http_status_for(exc) == 403


def test_domain_error_is_catchable_as_exception() -> None:
    """DomainError 是 Exception 子类，可被 ``except DomainError`` 捕获所有子类。"""
    raised: list[DomainError] = []
    for exc_cls in (
        ArgumentError,
        PermissionDeniedError,
        VersionConflictError,
        GitConflictError,
        ExternalDependencyError,
    ):
        try:
            raise exc_cls("x")
        except DomainError as exc:
            raised.append(exc)
    assert len(raised) == 5


def test_domain_error_handler_registered_for_all_subclasses() -> None:
    """``register_error_handlers`` 注册的处理器可捕获所有 DomainError 子类。"""
    app = FastAPI()

    @app.get("/raise")
    async def _raise() -> None:
        raise NotFoundError("missing task", context={"task_id": "t-1"})

    register_error_handlers(app)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/raise")
    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["error_code"] == "NOT_FOUND"
    assert err["details"] == {"task_id": "t-1"}


def test_domain_error_handler_is_callable_directly() -> None:
    """``domain_error_handler`` 可被直接 await，无需经 FastAPI 路由。"""

    class _FakeRequest:
        def __init__(self, headers: dict[str, str] | None = None) -> None:
            self.headers = headers or {}

    import asyncio

    exc = GitEventRejectedError("dup", reason_code=ReasonCode.EVENT_DUPLICATE)
    request = _FakeRequest({"traceparent": "00-traceXYZ-span1-01"})

    response = asyncio.run(domain_error_handler(request, exc))  # type: ignore[arg-type]
    assert response.status_code == 409
    import json

    body = json.loads(response.body)
    assert body["error"]["error_code"] == "GIT_EVENT_REJECTED"
    assert body["error"]["reason_code"] == "EVENT_DUPLICATE"
    assert body["error"]["trace_id"] == "traceXYZ"
