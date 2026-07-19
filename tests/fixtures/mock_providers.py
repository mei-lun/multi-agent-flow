"""Mock Model/Tool Adapter：不依赖真实模型 API Key。

实现 ``ProviderAdapter`` / ``ModelAdapter`` / ``ToolAdapter`` Protocol，
返回固定成功响应，不发起任何网络调用，满足「测试不依赖真实模型 Key」的
验收标准。供模型连接、用量、fallback、Tool 调用与 Agent loop 测试复用。
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from maf_contracts.model import (
    CanonicalMessage,
    ModelUsage,
    UnifiedModelRequest,
    UnifiedModelResponse,
)


class MockModelAdapter:
    """假模型 Adapter，返回固定成功响应，不发起网络调用。

    通过 ``invoke_count`` / ``last_request`` 暴露调用记录，便于断言。
    """

    adapter_type = "mock"

    def __init__(self, *, response_text: str = "mock-response") -> None:
        self._response_text = response_text
        self.invoke_count = 0
        self.last_request: UnifiedModelRequest | None = None

    async def probe(self, connection: dict[str, Any]) -> dict[str, Any]:
        """返回固定脱敏连接检查结果。"""
        return {"ok": True, "provider": "mock", "latency_ms": 0}

    async def list_models(self, connection: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"name": "mock-model", "context_window": 4096}]

    async def invoke(
        self,
        connection: dict[str, Any],
        model_name: str,
        request: UnifiedModelRequest,
    ) -> UnifiedModelResponse:
        self.invoke_count += 1
        self.last_request = request
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
        """产生规范化流增量；按空格切分固定响应。"""
        for chunk in self._response_text.split():
            yield {"delta": {"content": chunk + " "}}

    def normalize_error(self, error: Exception) -> dict[str, Any]:
        return {"code": "MOCK_ERROR", "retryable": False, "category": "client"}


class MockToolAdapter:
    """假 Tool Adapter，回显参数，不执行真实副作用。"""

    adapter_type = "mock"

    def __init__(self) -> None:
        self.invoke_count = 0

    async def invoke(
        self,
        definition: dict[str, Any],
        arguments: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        self.invoke_count += 1
        return {
            "ok": True,
            "echo": dict(arguments),
            "definition": dict(definition),
            "timeout_seconds": timeout_seconds,
        }

    async def cancel(self, external_call_id: str) -> None:
        return None


__all__ = ["MockModelAdapter", "MockToolAdapter"]
