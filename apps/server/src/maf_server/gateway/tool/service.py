"""Tool authorization contract and node-local execution gateway."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol
from maf_contracts.common import ExecutionContext
from maf_contracts.tool import *
from maf_policy.validators import validate_url


class ToolGateway(Protocol):
    async def list_allowed(self, context: ExecutionContext) -> ToolListResponse:
        """返回 Role Snapshot 与 Git task grant 交集中的 Tool，不返回隐藏工具。"""
        ...

    async def call(
        self, context: ExecutionContext, tool_key: str, request: ToolCallRequest
    ) -> ToolCallResult:
        """Validate, execute, or suspend an authorized Tool call."""
        ...

    async def get_call(self, context: ExecutionContext, call_id: str) -> ToolCallView:
        """Read a call belonging to the same assignment."""
        ...

    async def cancel_call(
        self, context: ExecutionContext, call_id: str, request: CancelToolCallRequest
    ) -> ToolCallView:
        """Idempotently cancel an active or approval-waiting call."""
        ...


@dataclass(frozen=True)
class LocalToolDefinition:
    key: str
    version: int
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    risk_level: str
    adapter_type: str
    adapter: Any
    approval_mode: str = "NONE"
    allowed_hosts: tuple[str, ...] = ()
    max_output_bytes: int = 1024 * 1024


BlockReporter = Callable[[dict[str, Any]], Awaitable[str]]
PolicyEvaluator = Callable[[ExecutionContext, LocalToolDefinition, dict[str, Any]], Awaitable[dict[str, Any]]]


class LocalToolGateway:
    """Execute explicitly registered tools inside one node process.

    It exposes no network endpoint and receives no Secret store.  Consequently
    a task container can only see descriptors returned by ``list_allowed`` and
    never obtains credentials for other tools.
    """

    def __init__(
        self,
        definitions: list[LocalToolDefinition],
        *,
        policy_evaluator: PolicyEvaluator | None = None,
        block_reporter: BlockReporter | None = None,
    ) -> None:
        self._definitions = {(item.key, item.version): item for item in definitions}
        self._policy_evaluator = policy_evaluator
        self._block_reporter = block_reporter
        self._calls: dict[str, ToolCallView] = {}
        self._call_definitions: dict[str, LocalToolDefinition] = {}
        self._by_key: dict[tuple[str, str], str] = {}
        self.audit: list[dict[str, Any]] = []

    async def list_allowed(self, context: ExecutionContext) -> ToolListResponse:
        grants = set(context.get("granted_tool_keys", []))
        tools = [
            ToolDescriptor(
                key=item.key,
                version=item.version,
                name=item.name,
                description=item.description,
                input_schema=dict(item.input_schema),
                output_schema=dict(item.output_schema),
                risk_level=item.risk_level,
            )
            for item in self._definitions.values()
            if item.key in grants or f"{item.key}:{item.version}" in grants
        ]
        return ToolListResponse(
            attempt_id=context["assignment_id"],
            tools=sorted(tools, key=lambda value: (value["key"], value["version"])),
        )

    async def call(self, context: ExecutionContext, tool_key: str, request: ToolCallRequest) -> ToolCallResult:
        identity = (request["attempt_id"], request["call_key"])
        existing_id = self._by_key.get(identity)
        if existing_id:
            existing = self._calls[existing_id].get("result")
            if existing is not None:
                return existing
        definition = self._definitions.get((tool_key, request["tool_version"]))
        grants = set(context.get("granted_tool_keys", []))
        if definition is None or (
            tool_key not in grants and f"{tool_key}:{request['tool_version']}" not in grants
        ):
            raise PermissionError("tool version is not granted to this assignment")
        arguments = request.get("arguments")
        if not isinstance(arguments, dict) or not _matches_schema(arguments, definition.input_schema):
            raise ValueError("tool input does not match its JSON Schema")
        if definition.adapter_type == "HTTP":
            method = str(arguments.get("method", "GET")).upper()
            url = arguments.get("url")
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                raise ValueError("HTTP method is not allowed")
            if not isinstance(url, str) or not validate_url(url, list(definition.allowed_hosts)):
                raise PermissionError("HTTP URL is outside the public allowlist")
        if self._policy_evaluator is not None:
            decision = await self._policy_evaluator(context, definition, dict(arguments))
            if not decision.get("allowed", False):
                raise PermissionError(str(decision.get("reason", "policy denied")))
            constrained = decision.get("arguments")
            if constrained is not None:
                if not isinstance(constrained, dict) or not _matches_schema(constrained, definition.input_schema):
                    raise ValueError("policy produced invalid constrained arguments")
                arguments = constrained
        call_id = f"tool-call-{uuid.uuid4()}"
        self._by_key[identity] = call_id
        if definition.approval_mode == "CENTRAL":
            inbox_id = None
            if self._block_reporter is not None:
                inbox_id = await self._block_reporter({
                    "task_id": context["task_id"],
                    "assignment_id": context["assignment_id"],
                    "tool_key": tool_key,
                    "tool_version": request["tool_version"],
                    "call_id": call_id,
                })
            result = _tool_result(call_id, "WAITING_APPROVAL", approval_id=inbox_id)
            self._store_call(context, definition, call_id, result)
            return result
        started = time.monotonic()
        try:
            output = await asyncio.wait_for(
                definition.adapter.invoke(
                    {"key": definition.key, "version": definition.version},
                    dict(arguments),
                    request["timeout_seconds"],
                ),
                timeout=request["timeout_seconds"],
            )
            if not isinstance(output, dict) or not _matches_schema(output, definition.output_schema):
                raise ValueError("tool output does not match its JSON Schema")
            if len(json.dumps(output, separators=(",", ":")).encode("utf-8")) > definition.max_output_bytes:
                raise ValueError("tool output exceeds size limit")
            final_url = output.get("final_url") if definition.adapter_type == "HTTP" else None
            if final_url is not None and not validate_url(final_url, list(definition.allowed_hosts)):
                raise PermissionError("HTTP redirect target is not allowed")
            result = _tool_result(call_id, "COMPLETED", output=output, duration_ms=int((time.monotonic() - started) * 1000))
        except asyncio.CancelledError:
            await definition.adapter.cancel(call_id)
            raise
        except Exception as exc:
            result = _tool_result(
                call_id,
                "FAILED",
                duration_ms=int((time.monotonic() - started) * 1000),
                error={"code": "TOOL_CALL_FAILED", "message": _safe_tool_error(exc)},
            )
        self._store_call(context, definition, call_id, result)
        return result

    async def get_call(self, context: ExecutionContext, call_id: str) -> ToolCallView:
        value = self._calls[call_id]
        if value["attempt_id"] != context["assignment_id"]:
            raise PermissionError("tool call belongs to another assignment")
        return dict(value)

    async def cancel_call(self, context: ExecutionContext, call_id: str, request: CancelToolCallRequest) -> ToolCallView:
        value = await self.get_call(context, call_id)
        if value["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            return value
        definition = self._call_definitions[call_id]
        await definition.adapter.cancel(call_id)
        result = _tool_result(call_id, "CANCELLED")
        value["status"] = "CANCELLED"
        value["completed_at"] = datetime.now(timezone.utc).isoformat()
        value["result"] = result
        self._calls[call_id] = value
        return dict(value)

    def _store_call(self, context: ExecutionContext, definition: LocalToolDefinition, call_id: str, result: ToolCallResult) -> None:
        now = datetime.now(timezone.utc).isoformat()
        view = ToolCallView(
            call_id=call_id,
            attempt_id=context["assignment_id"],
            tool_key=definition.key,
            status=result["status"],
            created_at=now,
            completed_at=now if result["status"] != "WAITING_APPROVAL" else None,
            result=result,
        )
        self._calls[call_id] = view
        self._call_definitions[call_id] = definition
        self.audit.append({"call_id": call_id, "task_id": context["task_id"], "tool_key": definition.key, "status": result["status"], "at": now})


def _matches_schema(value: Any, schema: dict[str, Any]) -> bool:
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not all(key in value for key in required):
            return False
        if schema.get("additionalProperties") is False and any(key not in properties for key in value):
            return False
        return all(key not in value or _matches_schema(value[key], child) for key, child in properties.items())
    if kind == "array":
        return isinstance(value, list) and all(_matches_schema(item, schema.get("items", {})) for item in value)
    if kind == "string":
        return isinstance(value, str) and value in schema.get("enum", [value])
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    return False


def _tool_result(call_id: str, status: str, *, output: dict[str, Any] | None = None, approval_id: str | None = None, duration_ms: int = 0, error: dict[str, Any] | None = None) -> ToolCallResult:
    return ToolCallResult(
        call_id=call_id,
        status=status,
        output=output,
        output_artifact_version_ids=[],
        approval_inbox_item_id=approval_id,
        duration_ms=duration_ms,
        error=error,
    )


def _safe_tool_error(exc: Exception) -> str:
    message = str(exc)
    if any(value in message.lower() for value in ("secret", "token", "authorization", "api_key", "bearer")):
        return "tool call failed (redacted)"
    return message[:240]


__all__ = ["LocalToolDefinition", "LocalToolGateway", "ToolGateway"]
