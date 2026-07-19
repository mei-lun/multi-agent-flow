"""Anthropic Claude Provider Adapter（基于 httpx）。

TASK-038 范围：
- 覆盖 Anthropic Claude 系列模型（claude-3-5-sonnet 等）；
- 凭据从 ``connection_config["api_key"]`` 注入，经 ``x-api-key`` 头传递，
  不在 Adapter 内访问 ``SecretService``；
- ``invoke``/``stream``/``probe`` 经 httpx 调用 Anthropic Messages API；
- ``list_models`` 返回固定已知模型（Anthropic 未提供 list 端点）；
- ``normalize_error`` 把 Anthropic 错误类型统一为 ``code``/``category``/``retryable``。

设计依据：
- 《多 Agent 协同工具系统设计文档》§11.3 首批 Adapter、§14.6 模型错误码。
- TASK-038 验收：Provider 错误统一为 code/category/retryable；异常和日志不含 Key。

安全约束：
- ``api_key`` 仅出现在 ``x-api-key`` 请求头，绝不写入 URL query、日志、异常或响应；
- 错误 message 不含 ``api_key`` 与完整 URL。
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

import httpx

from maf_contracts.model import (
    CanonicalMessage,
    ModelUsage,
    UnifiedModelRequest,
    UnifiedModelResponse,
)


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: Anthropic API 版本头（与官方 SDK 默认一致）。
_ANTHROPIC_VERSION: str = "2023-06-01"

#: Anthropic 已知模型固定列表（Anthropic 未提供 list models 端点）。
_KNOWN_MODELS: list[dict[str, Any]] = [
    {"name": "claude-3-5-sonnet-20241022", "context_window": 200000},
    {"name": "claude-3-5-haiku-20241022", "context_window": 200000},
    {"name": "claude-3-opus-20240229", "context_window": 200000},
    {"name": "claude-3-sonnet-20240229", "context_window": 200000},
    {"name": "claude-3-haiku-20240307", "context_window": 200000},
]

#: Anthropic error.type → (code, category, retryable) 映射。
_ANTHROPIC_ERROR_MAP: dict[str, tuple[str, str, bool]] = {
    "invalid_request_error": ("INVALID_REQUEST", "client", False),
    "authentication_error": ("AUTHENTICATION_FAILED", "client", False),
    "permission_error": ("PERMISSION_DENIED", "client", False),
    "not_found_error": ("NOT_FOUND", "client", False),
    "rate_limit_error": ("RATE_LIMITED", "server", True),
    "overloaded_error": ("PROVIDER_OVERLOADED", "server", True),
    "api_error": ("PROVIDER_ERROR", "server", True),
}

#: HTTP 状态码 → (code, category, retryable) 兜底映射。
_HTTP_ERROR_MAP: dict[int, tuple[str, str, bool]] = {
    400: ("INVALID_REQUEST", "client", False),
    401: ("AUTHENTICATION_FAILED", "client", False),
    403: ("PERMISSION_DENIED", "client", False),
    404: ("NOT_FOUND", "client", False),
    408: ("TIMEOUT", "server", True),
    413: ("REQUEST_TOO_LARGE", "client", False),
    429: ("RATE_LIMITED", "server", True),
    500: ("PROVIDER_ERROR", "server", True),
    502: ("BAD_GATEWAY", "server", True),
    503: ("SERVICE_UNAVAILABLE", "server", True),
    504: ("GATEWAY_TIMEOUT", "server", True),
}


def _default_timeout(timeout_seconds: int) -> httpx.Timeout:
    """构造 httpx 超时配置。"""
    return httpx.Timeout(timeout_seconds if timeout_seconds > 0 else 60.0)


def _extract_api_key(connection: dict[str, Any]) -> str:
    """从 connection 中提取 api_key；缺失抛 ``KeyError``。"""
    api_key = connection.get("api_key")
    if not isinstance(api_key, str) or not api_key:
        raise KeyError("connection.api_key 缺失")
    return api_key


def _extract_api_base(connection: dict[str, Any]) -> str:
    """从 connection 中提取 api_base；默认 Anthropic 官方端点。"""
    api_base = connection.get("api_base")
    if not isinstance(api_base, str) or not api_base:
        return "https://api.anthropic.com"
    return api_base.rstrip("/")


def _redact_url(url: str) -> str:
    """脱敏 URL：移除 query 段。"""
    if "?" in url:
        return url.split("?", 1)[0] + "?***"
    return url


class AnthropicProviderAdapter:
    """Anthropic Claude Provider Adapter。

    凭据注入：
    - ``api_key`` 从 ``connection_config["api_key"]`` 读取，经 ``x-api-key`` 头传递；
    - 绝不进入日志、异常或响应；``normalize_error`` 输出不含 ``api_key``。

    生命周期：
    - 实例化时不建立连接；每次方法调用创建短期 ``httpx.AsyncClient``；
    - ``connection`` 参数可覆盖构造时的 ``connection_config``。
    """

    adapter_type: str = "anthropic"

    def __init__(self, *, connection_config: dict[str, Any] | None = None) -> None:
        self._default_connection: dict[str, Any] = (
            dict(connection_config) if connection_config else {}
        )

    # ------------------------------------------------------------------ #
    # ProviderAdapter 实现
    # ------------------------------------------------------------------ #

    async def probe(self, connection: dict[str, Any]) -> dict[str, Any]:
        """用已解析凭据测试连接，返回脱敏检查结果。"""
        conn = self._merge_connection(connection)
        start = time.monotonic()
        try:
            api_base = _extract_api_base(conn)
            api_key = _extract_api_key(conn)
            # 最小 messages 调用验证凭据可用性
            payload = {
                "model": conn.get("model_id", "claude-3-5-haiku-20241022"),
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
            async with httpx.AsyncClient(timeout=_default_timeout(10)) as client:
                resp = await client.post(
                    f"{api_base}/v1/messages",
                    json=payload,
                    headers=self._auth_headers(api_key, conn),
                )
            latency_ms = int((time.monotonic() - start) * 1000)
            if resp.status_code == 200:
                return {
                    "ok": True,
                    "provider": self.adapter_type,
                    "latency_ms": latency_ms,
                    "api_base": _redact_url(api_base),
                }
            err = self._http_error(resp.status_code, resp.text)
            return {
                "ok": False,
                "provider": self.adapter_type,
                "latency_ms": latency_ms,
                "api_base": _redact_url(api_base),
                "error": err,
            }
        except Exception as exc:
            return {
                "ok": False,
                "provider": self.adapter_type,
                "latency_ms": int((time.monotonic() - start) * 1000),
                "api_base": _redact_url(str(conn.get("api_base", ""))),
                "error": self.normalize_error(exc),
            }

    async def list_models(self, connection: dict[str, Any]) -> list[dict[str, Any]]:
        """返回 Anthropic 已知模型固定列表（远端未提供 list 端点）。"""
        return [dict(m) for m in _KNOWN_MODELS]

    async def invoke(
        self,
        connection: dict[str, Any],
        model_name: str,
        request: UnifiedModelRequest,
    ) -> UnifiedModelResponse:
        """执行非流式 Messages 调用并归一化响应。"""
        conn = self._merge_connection(connection)
        start = time.monotonic()
        call_id = request.get("call_key") or f"call-{int(start * 1000)}"
        try:
            api_base = _extract_api_base(conn)
            api_key = _extract_api_key(conn)
            payload = self._build_messages_payload(model_name, request, stream=False)
            timeout = int(request.get("timeout_seconds") or 60)
            async with httpx.AsyncClient(timeout=_default_timeout(timeout)) as client:
                resp = await client.post(
                    f"{api_base}/v1/messages",
                    json=payload,
                    headers=self._auth_headers(api_key, conn),
                )
            latency_ms = int((time.monotonic() - start) * 1000)
            if resp.status_code != 200:
                err = self._http_error(resp.status_code, resp.text)
                return self._error_response(call_id, model_name, latency_ms, err)
            return self._parse_messages_response(
                resp.json(), call_id, model_name, latency_ms
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            err = self.normalize_error(exc)
            return self._error_response(call_id, model_name, latency_ms, err)

    async def stream(
        self,
        connection: dict[str, Any],
        model_name: str,
        request: UnifiedModelRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        """产生规范化流增量；调用取消时关闭底层连接。"""
        conn = self._merge_connection(connection)
        api_base = _extract_api_base(conn)
        api_key = _extract_api_key(conn)
        payload = self._build_messages_payload(model_name, request, stream=True)
        timeout = int(request.get("timeout_seconds") or 60)
        async with httpx.AsyncClient(timeout=_default_timeout(timeout)) as client:
            async with client.stream(
                "POST",
                f"{api_base}/v1/messages",
                json=payload,
                headers=self._auth_headers(api_key, conn),
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    err = self._http_error(
                        resp.status_code, body.decode("utf-8", "replace")
                    )
                    yield {"error": err}
                    return
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data = line[6:].strip()
                    elif line.startswith("data:"):
                        data = line[5:].strip()
                    else:
                        continue
                    if not data:
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = self._extract_delta(event)
                    if delta is not None:
                        yield delta

    def normalize_error(self, error: Exception) -> dict[str, Any]:
        """映射为稳定 code/retryable/category，并移除敏感信息。"""
        if isinstance(error, httpx.TimeoutException):
            return {
                "code": "TIMEOUT",
                "retryable": True,
                "category": "server",
                "message": "request timed out",
            }
        if isinstance(error, httpx.ConnectError):
            return {
                "code": "CONNECT_FAILED",
                "retryable": True,
                "category": "server",
                "message": "failed to connect to provider",
            }
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            code, category, retryable = _HTTP_ERROR_MAP.get(
                status, ("PROVIDER_ERROR", "server", True)
            )
            return {
                "code": code,
                "retryable": retryable,
                "category": category,
                "message": f"provider returned {status}",
            }
        msg = str(error)
        if any(k in msg.lower() for k in ("api_key", "x-api-key", "secret", "token", "bearer")):
            msg = "provider error"
        return {
            "code": "PROVIDER_ERROR",
            "retryable": True,
            "category": "server",
            "message": msg,
        }

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _merge_connection(self, connection: dict[str, Any]) -> dict[str, Any]:
        """合并默认 connection_config 与本次调用的 connection（后者优先）。"""
        merged = dict(self._default_connection)
        if isinstance(connection, dict):
            merged.update(connection)
        return merged

    @staticmethod
    def _auth_headers(api_key: str, connection: dict[str, Any]) -> dict[str, str]:
        """构造 Anthropic 认证头；``api_key`` 仅出现在 ``x-api-key`` 头。"""
        headers: dict[str, str] = {
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        extra = connection.get("extra_headers")
        if isinstance(extra, dict):
            for k, v in extra.items():
                if isinstance(k, str) and isinstance(v, str):
                    headers[k] = v
        return headers

    @staticmethod
    def _build_messages_payload(
        model_name: str, request: UnifiedModelRequest, *, stream: bool
    ) -> dict[str, Any]:
        """把 UnifiedModelRequest 映射为 Anthropic Messages payload。

        Anthropic 区分 system 与 user/assistant 消息：system 单独字段，
        其余进 messages 列表。
        """
        messages: list[dict[str, Any]] = []
        system_text: str = ""
        for msg in request.get("messages", []):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "system":
                system_text = content if isinstance(content, str) else str(content)
                continue
            messages.append({"role": str(role), "content": content})
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "stream": stream,
        }
        if system_text:
            payload["system"] = system_text
        max_tokens = request.get("max_output_tokens")
        if isinstance(max_tokens, int) and max_tokens > 0:
            payload["max_tokens"] = max_tokens
        else:
            payload["max_tokens"] = 1024  # Anthropic 要求必填
        temperature = request.get("temperature")
        if isinstance(temperature, (int, float)):
            payload["temperature"] = temperature
        tools = request.get("tools")
        if isinstance(tools, list) and tools:
            payload["tools"] = tools
        return payload

    @staticmethod
    def _parse_messages_response(
        body: dict[str, Any],
        call_id: str,
        model_name: str,
        latency_ms: int,
    ) -> UnifiedModelResponse:
        """把 Anthropic Messages 响应归一化为 UnifiedModelResponse。"""
        content_blocks = body.get("content") if isinstance(body, dict) else None
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        if isinstance(content_blocks, list):
            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(str(block.get("text", "")))
                elif btype == "tool_use":
                    tool_calls.append(block)
        message: CanonicalMessage | None = None
        if text_parts or content_blocks is not None:
            message = CanonicalMessage(
                role="assistant",
                content="".join(text_parts),
            )
        usage_body = body.get("usage") if isinstance(body, dict) else None
        if isinstance(usage_body, dict):
            usage = ModelUsage(
                input_tokens=int(usage_body.get("input_tokens", 0) or 0),
                output_tokens=int(usage_body.get("output_tokens", 0) or 0),
                cached_input_tokens=0,
                estimated_cost="0",
                currency="USD",
            )
        else:
            usage = ModelUsage(
                input_tokens=0,
                output_tokens=0,
                cached_input_tokens=0,
                estimated_cost="0",
                currency="USD",
            )
        stop_reason = body.get("stop_reason") if isinstance(body, dict) else None
        provider_request_id = None
        if isinstance(body, dict):
            provider_request_id = body.get("id")
        return UnifiedModelResponse(
            call_id=call_id,
            status="COMPLETED",
            model_profile_id=model_name,
            provider_request_id=str(provider_request_id) if provider_request_id else None,
            message=message,
            tool_calls=tool_calls,
            usage=usage,
            latency_ms=latency_ms,
            finish_reason=stop_reason,
            error=None,
        )

    @staticmethod
    def _extract_delta(event: dict[str, Any]) -> dict[str, Any] | None:
        """从 Anthropic SSE event 中提取规范化增量。"""
        if not isinstance(event, dict):
            return None
        etype = event.get("type")
        if etype == "content_block_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict):
                return None
            text = delta.get("text")
            if text is None:
                return None
            return {"delta": {"content": text}}
        return None

    @staticmethod
    def _error_response(
        call_id: str, model_name: str, latency_ms: int, error: dict[str, Any]
    ) -> UnifiedModelResponse:
        """构造失败 UnifiedModelResponse。"""
        return UnifiedModelResponse(
            call_id=call_id,
            status="FAILED",
            model_profile_id=model_name,
            provider_request_id=None,
            message=None,
            tool_calls=[],
            usage=ModelUsage(
                input_tokens=0,
                output_tokens=0,
                cached_input_tokens=0,
                estimated_cost="0",
                currency="USD",
            ),
            latency_ms=latency_ms,
            finish_reason=None,
            error=error,
        )

    @staticmethod
    def _http_error(status: int, body: str) -> dict[str, Any]:
        """把 HTTP 错误归一化为稳定错误码；解析 Anthropic error.type 优先。"""
        code, category, retryable = _HTTP_ERROR_MAP.get(
            status, ("PROVIDER_ERROR", "server", True)
        )
        # 尝试解析 Anthropic error.type 以获得更精确的 code
        try:
            parsed = json.loads(body) if body else {}
            if isinstance(parsed, dict):
                err_obj = parsed.get("error")
                if isinstance(err_obj, dict):
                    err_type = err_obj.get("type")
                    if isinstance(err_type, str) and err_type in _ANTHROPIC_ERROR_MAP:
                        code, category, retryable = _ANTHROPIC_ERROR_MAP[err_type]
        except (json.JSONDecodeError, ValueError):
            pass
        return {
            "code": code,
            "retryable": retryable,
            "category": category,
            "message": f"provider returned {status}",
        }


__all__ = ["AnthropicProviderAdapter"]
