"""OpenAI 兼容协议 Provider Adapter（基于 httpx）。

TASK-038 范围：
- 覆盖 OpenAI、GLM、DeepSeek、MiniMax、Kimi 等兼容 ``/chat/completions``
  与 ``/models`` 端点的供应商；
- 通过 ``ProviderAdapterFactory.create_adapter("openai", connection_config)``
  创建；凭据从 ``connection_config["api_key"]`` 注入，不在 Adapter 内访问
  ``SecretService``；
- ``invoke``/``stream``/``probe``/``list_models`` 经 httpx 调用远端；
- ``normalize_error`` 把 HTTP/网络错误统一为稳定 ``code``/``category``/``retryable``。

设计依据：
- 《多 Agent 协同工具系统设计文档》§11.3 首批 Adapter ``OpenAICompatibleChatAdapter``、
  §2.4 LiteLLM 边界、§14.6 模型错误码。
- TASK-038 验收：Provider 错误统一为 code/category/retryable；异常和日志不含 Key。

实现说明：
- ``litellm`` 未在测试运行时安装，本 Adapter 直接使用 ``httpx``（已在依赖中），
  保持零额外依赖；后续可在 ``invoke`` 内替换为 LiteLLM SDK 而不影响 Protocol。
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
# 错误码常量（与 §14.6 对齐）
# --------------------------------------------------------------------------- #

#: 供应商错误码 → (category, retryable) 默认映射。
_HTTP_ERROR_MAP: dict[int, tuple[str, str, bool]] = {
    400: ("INVALID_REQUEST", "client", False),
    401: ("AUTHENTICATION_FAILED", "client", False),
    403: ("PERMISSION_DENIED", "client", False),
    404: ("NOT_FOUND", "client", False),
    408: ("TIMEOUT", "server", True),
    413: ("REQUEST_TOO_LARGE", "client", False),
    422: ("UNPROCESSABLE_ENTITY", "client", False),
    429: ("RATE_LIMITED", "server", True),
    500: ("PROVIDER_ERROR", "server", True),
    502: ("BAD_GATEWAY", "server", True),
    503: ("SERVICE_UNAVAILABLE", "server", True),
    504: ("GATEWAY_TIMEOUT", "server", True),
}


def _default_timeout(timeout_seconds: int) -> httpx.Timeout:
    """构造 httpx 超时配置；默认沿用请求 ``timeout_seconds``。"""
    return httpx.Timeout(timeout_seconds if timeout_seconds > 0 else 60.0)


def _extract_api_key(connection: dict[str, Any]) -> str:
    """从 connection 中提取 api_key；缺失抛 ``KeyError``（由调用方归一化）。"""
    api_key = connection.get("api_key")
    if not isinstance(api_key, str) or not api_key:
        raise KeyError("connection.api_key 缺失")
    return api_key


def _extract_api_base(connection: dict[str, Any]) -> str:
    """从 connection 中提取 api_base；缺失抛 ``KeyError``。"""
    api_base = connection.get("api_base")
    if not isinstance(api_base, str) or not api_base:
        raise KeyError("connection.api_base 缺失")
    return api_base.rstrip("/")


def _redact_url(url: str) -> str:
    """脱敏 URL：移除 query 中的可能凭据，保留 scheme/host/path。"""
    # 简单脱敏：截断 query 段
    if "?" in url:
        return url.split("?", 1)[0] + "?***"
    return url


class OpenAICompatibleAdapter:
    """OpenAI 兼容协议 Adapter（GLM/DeepSeek/MiniMax/Kimi 等）。

    凭据注入：
    - ``api_key`` 从 ``connection_config["api_key"]`` 读取，绝不进入实例属性
      之外的日志、异常或响应；
    - HTTP 请求头使用 ``Authorization: Bearer {api_key}``，不写入 query 参数；
    - ``normalize_error`` 输出的 message 不含 ``api_key`` 与完整 URL。

    生命周期：
    - 实例化时不建立连接；每次 ``invoke``/``stream``/``probe``/``list_models``
      创建短期 ``httpx.AsyncClient``，调用结束即关闭，避免连接泄漏；
    - ``connection_config`` 在构造时保存为默认；每次方法调用的 ``connection``
      参数若提供则覆盖默认（支持凭据轮换后复用 Adapter）。
    """

    adapter_type: str = "openai_compatible"

    def __init__(self, *, connection_config: dict[str, Any] | None = None) -> None:
        self._default_connection: dict[str, Any] = (
            dict(connection_config) if connection_config else {}
        )

    # ------------------------------------------------------------------ #
    # ProviderAdapter 实现
    # ------------------------------------------------------------------ #

    async def probe(self, connection: dict[str, Any]) -> dict[str, Any]:
        """用已解析凭据测试连接，返回脱敏检查结果。

        输出 ``ok``/``provider``/``latency_ms``/``api_base``（脱敏）；
        失败时 ``ok=False`` 并带 ``error``（稳定码），不含 ``api_key``。
        """
        conn = self._merge_connection(connection)
        start = time.monotonic()
        try:
            api_base = _extract_api_base(conn)
            api_key = _extract_api_key(conn)
            async with httpx.AsyncClient(timeout=_default_timeout(10)) as client:
                resp = await client.get(
                    f"{api_base}/models",
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
            err = self._http_error(resp.status_code, resp.text, api_base)
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
        """列出远端声明模型；供应商不支持时返回空列表。"""
        conn = self._merge_connection(connection)
        try:
            api_base = _extract_api_base(conn)
            api_key = _extract_api_key(conn)
            async with httpx.AsyncClient(timeout=_default_timeout(10)) as client:
                resp = await client.get(
                    f"{api_base}/models",
                    headers=self._auth_headers(api_key, conn),
                )
            if resp.status_code != 200:
                return []
            data = resp.json()
            models = data.get("data") if isinstance(data, dict) else None
            if not isinstance(models, list):
                return []
            result: list[dict[str, Any]] = []
            for item in models:
                if not isinstance(item, dict):
                    continue
                model_id = item.get("id") or item.get("name")
                if not model_id:
                    continue
                result.append(
                    {
                        "name": str(model_id),
                        "context_window": item.get("context_length")
                        or item.get("context_window")
                        or 0,
                    }
                )
            return result
        except Exception:
            return []

    async def invoke(
        self,
        connection: dict[str, Any],
        model_name: str,
        request: UnifiedModelRequest,
    ) -> UnifiedModelResponse:
        """执行非流式 chat/completions 调用并归一化响应。"""
        conn = self._merge_connection(connection)
        start = time.monotonic()
        call_id = request.get("call_key") or f"call-{int(start * 1000)}"
        try:
            api_base = _extract_api_base(conn)
            api_key = _extract_api_key(conn)
            payload = self._build_chat_payload(model_name, request, stream=False)
            timeout = int(request.get("timeout_seconds") or 60)
            async with httpx.AsyncClient(timeout=_default_timeout(timeout)) as client:
                resp = await client.post(
                    f"{api_base}/chat/completions",
                    json=payload,
                    headers=self._auth_headers(api_key, conn),
                )
            latency_ms = int((time.monotonic() - start) * 1000)
            if resp.status_code != 200:
                err = self._http_error(resp.status_code, resp.text, api_base)
                return self._error_response(call_id, model_name, latency_ms, err)
            return self._parse_chat_response(resp.json(), call_id, model_name, latency_ms)
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
        payload = self._build_chat_payload(model_name, request, stream=True)
        timeout = int(request.get("timeout_seconds") or 60)
        async with httpx.AsyncClient(timeout=_default_timeout(timeout)) as client:
            async with client.stream(
                "POST",
                f"{api_base}/chat/completions",
                json=payload,
                headers=self._auth_headers(api_key, conn),
            ) as resp:
                if resp.status_code != 200:
                    # 流式错误：读取 body 后归一化并产出 error 事件
                    body = await resp.aread()
                    err = self._http_error(
                        resp.status_code, body.decode("utf-8", "replace"), api_base
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
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = self._extract_delta(chunk)
                    if delta is not None:
                        yield delta

    def normalize_error(self, error: Exception) -> dict[str, Any]:
        """映射为稳定 code/retryable/category，并移除请求头、URL 凭据。"""
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
        # 兜底：未知异常归一化为 server 错误，message 不含敏感字段
        msg = str(error)
        if any(k in msg.lower() for k in ("api_key", "authorization", "secret", "token", "bearer")):
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
        """构造认证头；``api_key`` 仅出现在 ``Authorization`` 头，不写入 query。"""
        headers: dict[str, str] = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        extra = connection.get("extra_headers")
        if isinstance(extra, dict):
            for k, v in extra.items():
                if isinstance(k, str) and isinstance(v, str):
                    headers[k] = v
        return headers

    @staticmethod
    def _build_chat_payload(
        model_name: str, request: UnifiedModelRequest, *, stream: bool
    ) -> dict[str, Any]:
        """把 UnifiedModelRequest 映射为 OpenAI chat/completions payload。"""
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": request.get("messages", []),
            "stream": stream,
        }
        max_tokens = request.get("max_output_tokens")
        if isinstance(max_tokens, int) and max_tokens > 0:
            payload["max_tokens"] = max_tokens
        temperature = request.get("temperature")
        if isinstance(temperature, (int, float)):
            payload["temperature"] = temperature
        tools = request.get("tools")
        if isinstance(tools, list) and tools:
            payload["tools"] = tools
        response_schema = request.get("response_schema")
        if isinstance(response_schema, dict):
            payload["response_format"] = response_schema
        return payload

    @staticmethod
    def _parse_chat_response(
        body: dict[str, Any],
        call_id: str,
        model_name: str,
        latency_ms: int,
    ) -> UnifiedModelResponse:
        """把 OpenAI chat/completions 响应归一化为 UnifiedModelResponse。"""
        choices = body.get("choices") if isinstance(body, dict) else None
        message: CanonicalMessage | None = None
        finish_reason: str | None = None
        tool_calls: list[dict[str, Any]] = []
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                msg = first.get("message")
                if isinstance(msg, dict):
                    content = msg.get("content", "")
                    message = CanonicalMessage(
                        role=str(msg.get("role", "assistant")),
                        content=content if isinstance(content, str) else str(content),
                    )
                    tc = msg.get("tool_calls")
                    if isinstance(tc, list):
                        tool_calls = [t for t in tc if isinstance(t, dict)]
                finish_reason = first.get("finish_reason")
        usage_body = body.get("usage") if isinstance(body, dict) else None
        if isinstance(usage_body, dict):
            usage = ModelUsage(
                input_tokens=int(usage_body.get("prompt_tokens", 0) or 0),
                output_tokens=int(usage_body.get("completion_tokens", 0) or 0),
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
            finish_reason=finish_reason,
            error=None,
        )

    @staticmethod
    def _extract_delta(chunk: dict[str, Any]) -> dict[str, Any] | None:
        """从 SSE chunk 中提取规范化增量。"""
        if not isinstance(chunk, dict):
            return None
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, dict):
            return None
        delta = first.get("delta")
        if not isinstance(delta, dict):
            return None
        content = delta.get("content")
        if content is None:
            return None
        return {"delta": {"content": content}}

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
    def _http_error(status: int, body: str, api_base: str) -> dict[str, Any]:
        """把 HTTP 错误归一化为稳定错误码；message 不含 ``api_key``。"""
        code, category, retryable = _HTTP_ERROR_MAP.get(
            status, ("PROVIDER_ERROR", "server", True)
        )
        return {
            "code": code,
            "retryable": retryable,
            "category": category,
            "message": f"provider returned {status}",
        }


__all__ = ["OpenAICompatibleAdapter"]
