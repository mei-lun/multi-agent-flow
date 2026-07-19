"""Runner-side facade for an in-process node-local Tool Gateway."""

from __future__ import annotations

from typing import Any

from maf_contracts.common import ExecutionContext
from maf_contracts.tool import CancelToolCallRequest, ToolCallRequest, ToolCallResult, ToolListResponse


class ToolClient:
    def __init__(self, gateway: Any, context: ExecutionContext) -> None:
        self._gateway = gateway
        self._context = dict(context)

    async def list_allowed(self) -> ToolListResponse:
        return await self._gateway.list_allowed(self._context)

    async def call(self, tool_key: str, request: ToolCallRequest) -> ToolCallResult:
        return await self._gateway.call(self._context, tool_key, request)

    async def cancel(self, call_id: str, reason: str) -> None:
        await self._gateway.cancel_call(
            self._context,
            call_id,
            CancelToolCallRequest(reason=reason, idempotency_key=f"cancel:{call_id}"),
        )


__all__ = ["ToolClient"]
