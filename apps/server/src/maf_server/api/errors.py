"""把领域错误统一映射为稳定 HTTP 错误负载。

API 层不得把异常堆栈或宿主机绝对路径返回客户端；本模块提供：
- ``ErrorResponse``：与 ``DomainError`` 字段对齐的 Pydantic 模型，不含堆栈；
- ``http_status_for``：``DomainError`` → HTTP 状态码映射；
- ``to_error_response``：``DomainError`` → ``ErrorResponse`` 构造器；
- ``domain_error_handler``：FastAPI 异常处理器，序列化 ``ErrorResponse`` 并隐藏堆栈；
- ``register_error_handlers``：在 FastAPI 应用上注册领域异常处理器。

映射规则遵循《接口设计与实现规范》第 4 节。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from maf_domain.errors import (
    DomainError,
    ErrorCode,
    RateLimitedError,
)


class ErrorResponse(BaseModel):
    """API 错误响应负载，不含堆栈。

    - ``error_code``：稳定错误码，取自 ``ErrorCode``；
    - ``reason_code``：稳定子原因，``EventDecision`` 拒绝原因、Policy 拒绝原因等；
    - ``message``：人类可读说明，不含敏感信息；
    - ``retryable``：客户端能否按相同参数重试；
    - ``details``：附加键值对（如 ``task_id``、``assignment_epoch``），不含敏感信息；
    - ``trace_id``：请求追踪 ID，对应 ``traceparent``。
    """

    error_code: str = Field(..., description="稳定错误码")
    reason_code: str | None = Field(None, description="稳定子原因；EventDecision/Policy 使用")
    message: str = Field(..., description="人类可读错误说明，不含堆栈")
    retryable: bool = Field(False, description="客户端是否可按相同参数重试")
    details: dict[str, Any] | None = Field(None, description="附加键值对；不含敏感信息")
    trace_id: str | None = Field(None, description="请求追踪 ID")


# ErrorCode → HTTP 状态码映射。遵循《接口设计与实现规范》第 4 节：
# 参数错误 400；未认证 401；无权限 403；不存在 404；
# 版本/幂等/Git 冲突 409；Schema 不通过 422；限额 429；
# 外部依赖失败 503；其他内部错误 500。
_STATUS_BY_ERROR_CODE: dict[ErrorCode, int] = {
    ErrorCode.ARGUMENT_INVALID: 400,
    ErrorCode.UNSUPPORTED_OPERATION: 400,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.TOOL_POLICY_DENIED: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.ALREADY_EXISTS: 409,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.GIT_CONFLICT: 409,
    ErrorCode.GIT_EVENT_REJECTED: 409,
    ErrorCode.STATE_TRANSITION_INVALID: 409,
    ErrorCode.SCHEMA_VALIDATION_FAILED: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.BUDGET_EXCEEDED: 429,
    ErrorCode.EXTERNAL_DEPENDENCY_FAILED: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}


def http_status_for(error: DomainError) -> int:
    """把领域错误映射为 HTTP 状态码。

    未知 ``ErrorCode`` 默认 500，避免暴露内部细节给客户端。
    """
    return _STATUS_BY_ERROR_CODE.get(error.error_code, 500)


def to_error_response(
    error: DomainError,
    *,
    trace_id: str | None = None,
) -> ErrorResponse:
    """把 ``DomainError`` 构造为 ``ErrorResponse``。

    本函数不读取 ``error.__traceback__``，不附加堆栈，确保不泄露内部信息。
    ``error.context`` 中的键值对原样放入 ``details``，调用方需保证不含敏感信息。
    """
    return ErrorResponse(
        error_code=error.error_code.value,
        reason_code=error.reason_code,
        message=error.message,
        retryable=error.retryable,
        details=dict(error.context) if error.context else None,
        trace_id=trace_id,
    )


def _extract_trace_id(request: Request) -> str | None:
    """从请求头 ``traceparent`` 中提取 trace_id（W3C Trace Context 简化解析）。

    ``traceparent`` 形如 ``00-<trace-id>-<span-id>-<flags>``。无法解析时返回 ``None``。
    """
    traceparent = request.headers.get("traceparent")
    if not traceparent:
        return None
    parts = traceparent.strip().split("-")
    if len(parts) >= 2 and parts[1]:
        return parts[1]
    return None


def _extra_headers(error: DomainError) -> dict[str, str]:
    """根据错误类型添加响应头，例如限流时返回 ``Retry-After``。"""
    headers: dict[str, str] = {}
    if isinstance(error, RateLimitedError):
        retry_after = error.context.get("retry_after")
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            headers["Retry-After"] = str(int(retry_after))
    return headers


async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI 异常处理器：把 ``DomainError`` 转为 ``ErrorResponse`` JSON 响应。

    本处理器只序列化 ``ErrorResponse`` 的稳定字段，不包含异常堆栈、文件路径
    或内部状态，符合《接口设计与实现规范》第 4 节"对外错误使用稳定 error_code，
    不得把异常堆栈直接返回客户端"的要求。响应正文形如::

        {"error": {"error_code": "...", "message": "...", "retryable": false, ...}}

    Starlette ``add_exception_handler`` 要求第二个参数类型为 ``Exception``；
    实际仅通过 ``register_error_handlers`` 注册到 ``DomainError``，因此进入
    本处理器的一定是 ``DomainError`` 子类。
    """
    assert isinstance(exc, DomainError), "domain_error_handler 仅处理 DomainError"
    trace_id = _extract_trace_id(request)
    body = {"error": to_error_response(exc, trace_id=trace_id).model_dump(exclude_none=True)}
    return JSONResponse(
        status_code=http_status_for(exc),
        content=body,
        headers=_extra_headers(exc),
    )


def register_error_handlers(app: FastAPI) -> None:
    """在 FastAPI 应用上注册领域错误异常处理器。

    应在 ``apps/server/src/maf_server/main.py`` 创建应用后调用一次。
    """
    app.add_exception_handler(DomainError, domain_error_handler)


__all__ = [
    "ErrorResponse",
    "http_status_for",
    "to_error_response",
    "domain_error_handler",
    "register_error_handlers",
]
