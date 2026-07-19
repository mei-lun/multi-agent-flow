"""测试用 Mock Provider Adapter：不依赖真实模型 API Key 与网络。

TASK-038 范围：
- 提供一个最小可用的 ``ProviderAdapter`` 实现，供 ``ProviderAdapterFactory``
  与契约测试使用；
- ``invoke``/``stream``/``probe``/``list_models`` 均不发起任何网络调用，
  返回固定成功响应，满足「测试不依赖真实模型 Key」的验收标准；
- ``normalize_error`` 返回稳定的 ``code``/``category``/``retryable`` 三元组。

设计依据：
- 《多 Agent 协同工具系统设计文档》§11.3 ProviderAdapter、§2.4 LiteLLM 边界。
- 与 ``tests/fixtures/mock_providers.py::MockModelAdapter`` 思路一致，但本类
  位于生产包内，可被 ``gateway/model/adapters.py`` 的 Factory 直接注册和创建，
  无需 sys.path hack。
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from maf_contracts.model import (
    CanonicalMessage,
    ModelUsage,
    UnifiedModelRequest,
    UnifiedModelResponse,
)


class MockProviderAdapter:
    """假模型 Provider Adapter，返回固定成功响应，不发起网络调用。

    通过 ``invoke_count``/``last_request``/``last_connection`` 暴露调用记录，
    便于断言。``connection_config`` 中的 ``api_key`` 仅用于验证凭据注入路径，
    绝不进入响应、日志或异常。

    本类仅用于：
    - ``ProviderAdapterFactory`` 默认注册的 mock 路由；
    - 契约测试与集成测试；
    - 文档示例。
    生产路径应使用 ``OpenAICompatibleAdapter`` / ``AnthropicProviderAdapter``。
    """

    adapter_type: str = "mock"

    def __init__(
        self,
        *,
        response_text: str = "mock-response",
        connection_config: dict[str, Any] | None = None,
    ) -> None:
        self._response_text: str = response_text
        self._connection_config: dict[str, Any] = dict(connection_config) if connection_config else {}
        self.invoke_count: int = 0
        self.last_request: UnifiedModelRequest | None = None
        self.last_connection: dict[str, Any] | None = None

    # ------------------------------------------------------------------ #
    # ProviderAdapter 实现
    # ------------------------------------------------------------------ #

    async def probe(self, connection: dict[str, Any]) -> dict[str, Any]:
        """返回固定脱敏连接检查结果；不保存凭据，不发起网络调用。"""
        self.last_connection = self._sanitize_connection(connection)
        return {
            "ok": True,
            "provider": self.adapter_type,
            "latency_ms": 0,
            "api_base": connection.get("api_base") if isinstance(connection, dict) else None,
        }

    async def list_models(self, connection: dict[str, Any]) -> list[dict[str, Any]]:
        """返回固定模型列表；不发起网络调用。"""
        self.last_connection = self._sanitize_connection(connection)
        return [
            {"name": "mock-model", "context_window": 4096},
            {"name": "mock-model-large", "context_window": 32768},
        ]

    async def invoke(
        self,
        connection: dict[str, Any],
        model_name: str,
        request: UnifiedModelRequest,
    ) -> UnifiedModelResponse:
        """返回固定成功响应；记录调用次数与请求，不发起网络调用。"""
        self.invoke_count += 1
        self.last_request = request
        self.last_connection = self._sanitize_connection(connection)
        return UnifiedModelResponse(
            call_id=f"mock-{self.invoke_count}",
            status="COMPLETED",
            model_profile_id=model_name,
            provider_request_id=None,
            message=CanonicalMessage(role="assistant", content=self._response_text),
            tool_calls=[],
            usage=ModelUsage(
                input_tokens=1,
                output_tokens=1,
                cached_input_tokens=0,
                estimated_cost="0",
                currency="USD",
            ),
            latency_ms=0,
            finish_reason="stop",
            error=None,
        )

    async def stream(
        self,
        connection: dict[str, Any],
        model_name: str,
        request: UnifiedModelRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        """产生规范化流增量；按空格切分固定响应，不发起网络调用。"""
        self.last_request = request
        self.last_connection = self._sanitize_connection(connection)
        for chunk in self._response_text.split():
            yield {"delta": {"content": chunk + " "}}

    def normalize_error(self, error: Exception) -> dict[str, Any]:
        """映射为稳定 code/retryable/category，并移除敏感信息。"""
        return {
            "code": "MOCK_ERROR",
            "retryable": False,
            "category": "client",
            "message": str(error) if not self._contains_secret(error) else "mock error",
        }

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _sanitize_connection(connection: dict[str, Any]) -> dict[str, Any]:
        """移除 connection 中的凭据字段，仅保留脱敏副本用于断言。"""
        if not isinstance(connection, dict):
            return {}
        sanitized = dict(connection)
        for key in ("api_key", "authorization", "token", "secret", "credential_value"):
            if key in sanitized:
                sanitized[key] = "***REDACTED***"
        return sanitized

    @staticmethod
    def _contains_secret(error: Exception) -> bool:
        """检查异常消息是否可能包含敏感字段名。"""
        msg = str(error).lower()
        return any(k in msg for k in ("api_key", "authorization", "secret", "token"))


__all__ = ["MockProviderAdapter"]
