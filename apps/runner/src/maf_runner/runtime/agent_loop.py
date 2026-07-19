"""Bounded observe-think-act loop used by the node runtime.

The runner deliberately keeps this module free of provider-specific code.  A
``context`` may be a mapping (the form produced by the context builder) or an
object with equivalent attributes.  The loop only talks to the ``ModelClient``
and ``ToolClient`` protocols and fails closed when a response is malformed or
an untrusted tool is requested.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any


def _value(context: Any, name: str, default: Any = None) -> Any:
    """Read a context value from either a mapping or an attribute object."""

    if isinstance(context, Mapping):
        return context.get(name, default)
    return getattr(context, name, default)


def _number(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _cancelled(context: Any) -> bool:
    event = _value(context, "cancel_event") or _value(context, "cancellation_event")
    if event is not None:
        is_set = getattr(event, "is_set", None)
        if callable(is_set) and is_set():
            return True
    callback = _value(context, "is_cancelled") or _value(context, "cancelled")
    if callable(callback):
        try:
            return bool(callback())
        except Exception:  # noqa: BLE001 - cancellation is fail-closed
            return True
    return bool(callback) if isinstance(callback, bool) else False


async def _invoke_with_deadline(client: Any, method: str, request: Any, remaining: float) -> Any:
    """Invoke a client method, applying the loop's remaining wall-clock budget."""

    fn = getattr(client, method)
    result = fn(*request) if isinstance(request, tuple) else fn(request)
    if not asyncio.iscoroutine(result):
        return result
    if remaining <= 0:
        raise TimeoutError("agent loop timeout exceeded")
    return await asyncio.wait_for(result, timeout=remaining)


def _tool_key(call: Mapping[str, Any]) -> str:
    return str(call.get("tool_key") or call.get("name") or call.get("key") or "")


def _tool_args(call: Mapping[str, Any]) -> dict[str, Any]:
    args = call.get("arguments", call.get("input", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return dict(args) if isinstance(args, Mapping) else {}


def _message_content(message: Any) -> Any:
    if isinstance(message, Mapping):
        return message.get("content")
    return getattr(message, "content", None)


def _validate_output(value: Any, contract: Any) -> tuple[bool, str | None]:
    """Perform a small deterministic subset of JSON Schema validation.

    The full artifact validator runs later in the submission pipeline.  This
    pre-check catches malformed model output before it is treated as success;
    it supports the contract fields used by the runner (type, required,
    properties, items and enum) without executing arbitrary expressions.
    """

    if contract is None:
        return True, None
    if not isinstance(contract, Mapping):
        return False, "output contract must be an object"
    if "enum" in contract and value not in contract.get("enum", []):
        return False, "output is not one of the allowed values"
    expected = contract.get("type")
    type_ok = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float, Decimal)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if isinstance(expected, str) and expected in type_ok and not type_ok[expected]:
        return False, f"output type must be {expected}"
    if isinstance(value, Mapping):
        required = contract.get("required", [])
        if isinstance(required, list):
            missing = [str(k) for k in required if k not in value]
            if missing:
                return False, f"missing required output fields: {', '.join(missing)}"
        properties = contract.get("properties", {})
        if isinstance(properties, Mapping):
            for key, schema in properties.items():
                if key in value:
                    ok, reason = _validate_output(value[key], schema)
                    if not ok:
                        return False, f"field {key}: {reason}"
    if isinstance(value, list) and isinstance(contract.get("items"), Mapping):
        for item in value:
            ok, reason = _validate_output(item, contract["items"])
            if not ok:
                return False, f"array item: {reason}"
    return True, None


def _decode_final(message: Any) -> Any:
    content = _message_content(message)
    if isinstance(content, (Mapping, list)):
        return content
    if not isinstance(content, str):
        return content
    text = content.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return content


async def run_agent(context: object) -> object:
    """Run a bounded observe-think-act loop.

    Context keys/attributes accepted by this function include ``model_client``,
    ``tool_client``, ``attempt_id``, ``messages``, ``tools``, ``response_schema``
    (or ``output_contract``), ``max_steps``, ``max_tool_calls``,
    ``timeout_seconds``, ``budget`` and a cancellation event/callback.  The
    return value is a JSON-serialisable dictionary with ``status`` (one of
    ``COMPLETED``, ``FAILED`` or ``CANCELLED``), output, usage and diagnostics.
    """

    model_client = _value(context, "model_client") or _value(context, "model")
    if model_client is None or not callable(getattr(model_client, "invoke", None)):
        return {"status": "FAILED", "error": {"code": "MODEL_CLIENT_MISSING"}}

    attempt_id = str(_value(context, "attempt_id", "attempt"))
    max_steps = _number(_value(context, "max_steps", 8), 8) or 1
    max_tool_calls = _number(_value(context, "max_tool_calls", 32), 32)
    timeout_seconds = float(_value(context, "timeout_seconds", 0) or 0)
    deadline = time.monotonic() + timeout_seconds if timeout_seconds > 0 else None
    budget = _value(context, "budget", {}) or {}
    max_output_tokens = _number(
        _value(context, "max_output_tokens", budget.get("max_output_tokens", 0)), 0
    )
    max_cost = _decimal(budget.get("max_cost_amount", _value(context, "max_cost_amount", "0")))
    enforce_cost = max_cost > 0

    messages = list(_value(context, "messages", []) or [])
    tools = list(_value(context, "tools", []) or [])
    tool_client = _value(context, "tool_client") or _value(context, "tools_client")
    progress = _value(context, "progress_reporter") or _value(context, "progress")
    output_contract = _value(context, "output_contract")
    if output_contract is None:
        output_contract = _value(context, "response_schema")

    model_usage = {"input_tokens": 0, "output_tokens": 0, "estimated_cost": "0"}
    tool_usage = {"calls": 0}
    allowed_keys: set[str] | None = None
    if tool_client is not None and not tools and callable(getattr(tool_client, "list_allowed", None)):
        try:
            listed = tool_client.list_allowed()
            if asyncio.iscoroutine(listed):
                listed = await listed
            if isinstance(listed, Mapping):
                tools = list(listed.get("tools") or [])
                # An empty response is an explicit empty allow-list.  Keep
                # ``None`` only when no ToolClient was available at all.
                allowed_keys = {
                    str(t.get("key")) for t in tools
                    if isinstance(t, Mapping) and t.get("key")
                }
        except Exception:  # noqa: BLE001 - unavailable tools must not be exposed
            tools = []
            allowed_keys = set()
    if tools:
        allowed_keys = {str(t.get("key")) for t in tools if isinstance(t, Mapping) and t.get("key")}

    final_message: Any = None
    for step in range(max_steps):
        if _cancelled(context):
            return {"status": "CANCELLED", "steps": step, "model_usage": model_usage, "tool_usage": tool_usage}
        remaining = (deadline - time.monotonic()) if deadline is not None else 3600.0
        if remaining <= 0:
            return {"status": "FAILED", "steps": step, "model_usage": model_usage, "tool_usage": tool_usage,
                    "error": {"code": "TIMEOUT"}}
        request = {
            "attempt_id": attempt_id,
            "call_key": f"{attempt_id}:model:{step}",
            "model_policy_id": str(_value(context, "model_policy_id", "")),
            "messages": messages,
            "tools": tools,
            "response_schema": output_contract,
            "temperature": _value(context, "temperature"),
            "max_output_tokens": max_output_tokens,
            "timeout_seconds": max(1, int(remaining)),
            "metadata": {"step": str(step)},
        }
        try:
            response = await _invoke_with_deadline(model_client, "invoke", request, remaining)
        except asyncio.TimeoutError:
            return {"status": "FAILED", "steps": step + 1, "model_usage": model_usage, "tool_usage": tool_usage,
                    "error": {"code": "TIMEOUT"}}
        except Exception as exc:  # noqa: BLE001 - provider errors are terminal, no implicit retries
            return {"status": "FAILED", "steps": step + 1, "model_usage": model_usage, "tool_usage": tool_usage,
                    "error": {"code": "MODEL_ERROR", "message": str(exc)}}

        usage = response.get("usage", {}) if isinstance(response, Mapping) else {}
        if not isinstance(usage, Mapping):
            usage = {}
        model_usage["input_tokens"] += _number(usage.get("input_tokens"))
        model_usage["output_tokens"] += _number(usage.get("output_tokens"))
        model_usage["estimated_cost"] = str(_decimal(model_usage["estimated_cost"]) + _decimal(usage.get("estimated_cost")))
        if max_output_tokens and model_usage["output_tokens"] > max_output_tokens:
            return {"status": "FAILED", "steps": step + 1, "model_usage": model_usage, "tool_usage": tool_usage,
                    "error": {"code": "OUTPUT_TOKEN_BUDGET_EXCEEDED"}}
        if enforce_cost and _decimal(model_usage["estimated_cost"]) > max_cost:
            return {"status": "FAILED", "steps": step + 1, "model_usage": model_usage, "tool_usage": tool_usage,
                    "error": {"code": "COST_BUDGET_EXCEEDED"}}
        if not isinstance(response, Mapping) or response.get("status") != "COMPLETED":
            error = response.get("error") if isinstance(response, Mapping) else None
            return {"status": "FAILED", "steps": step + 1, "model_usage": model_usage, "tool_usage": tool_usage,
                    "error": error or {"code": "MODEL_FAILED"}}

        final_message = response.get("message")
        if final_message is not None:
            messages.append(final_message)
        calls = response.get("tool_calls") or []
        if not isinstance(calls, list):
            return {"status": "FAILED", "steps": step + 1, "model_usage": model_usage, "tool_usage": tool_usage,
                    "error": {"code": "INVALID_TOOL_CALLS"}}
        if not calls:
            output = _decode_final(final_message)
            valid, reason = _validate_output(output, output_contract)
            if not valid:
                return {"status": "FAILED", "steps": step + 1, "model_usage": model_usage, "tool_usage": tool_usage,
                        "error": {"code": "OUTPUT_CONTRACT_INVALID", "message": reason}}
            if progress is not None and callable(getattr(progress, "flush", None)):
                try:
                    await progress.flush()
                except Exception:  # noqa: BLE001 - reporting must not alter result
                    pass
            return {"status": "COMPLETED", "steps": step + 1, "output": output,
                    "model_usage": model_usage, "tool_usage": tool_usage}
        if tool_client is None or not callable(getattr(tool_client, "call", None)):
            return {"status": "FAILED", "steps": step + 1, "model_usage": model_usage, "tool_usage": tool_usage,
                    "error": {"code": "TOOL_CLIENT_MISSING"}}
        for index, call in enumerate(calls):
            if not isinstance(call, Mapping):
                return {"status": "FAILED", "steps": step + 1, "model_usage": model_usage, "tool_usage": tool_usage,
                        "error": {"code": "INVALID_TOOL_CALL"}}
            if tool_usage["calls"] >= max_tool_calls:
                return {"status": "FAILED", "steps": step + 1, "model_usage": model_usage, "tool_usage": tool_usage,
                        "error": {"code": "TOOL_CALL_BUDGET_EXCEEDED"}}
            key = _tool_key(call)
            if not key or (allowed_keys is not None and key not in allowed_keys):
                return {"status": "FAILED", "steps": step + 1, "model_usage": model_usage, "tool_usage": tool_usage,
                        "error": {"code": "TOOL_NOT_AUTHORIZED", "tool_key": key}}
            remaining = (deadline - time.monotonic()) if deadline is not None else 3600.0
            try:
                tool_request = {
                    "attempt_id": attempt_id,
                    "tool_version": _number(call.get("tool_version", 1), 1),
                    "arguments": _tool_args(call),
                    "call_key": str(call.get("call_key") or f"{attempt_id}:tool:{tool_usage['calls']}"),
                    "timeout_seconds": max(1, int(remaining)),
                }
                result = await _invoke_with_deadline(tool_client, "call", (key, tool_request), remaining)
            except asyncio.TimeoutError:
                return {"status": "FAILED", "steps": step + 1, "model_usage": model_usage, "tool_usage": tool_usage,
                        "error": {"code": "TIMEOUT"}}
            except Exception as exc:  # noqa: BLE001 - tool failures are terminal for this attempt
                return {"status": "FAILED", "steps": step + 1, "model_usage": model_usage, "tool_usage": tool_usage,
                        "error": {"code": "TOOL_ERROR", "tool_key": key, "message": str(exc)}}
            tool_usage["calls"] += 1
            if not isinstance(result, Mapping):
                result = {"status": "FAILED", "output": None, "error": {"code": "INVALID_TOOL_RESULT"}}
            messages.append({
                "role": "tool",
                "name": key,
                "tool_call_id": str(call.get("call_id") or f"tool-{step}-{index}"),
                "content": json.dumps(result.get("output") if result.get("output") is not None else result,
                                       ensure_ascii=False, default=str),
            })
            if result.get("status") in {"FAILED", "CANCELLED"}:
                return {"status": "FAILED" if result.get("status") == "FAILED" else "CANCELLED",
                        "steps": step + 1, "model_usage": model_usage, "tool_usage": tool_usage,
                        "error": result.get("error") or {"code": "TOOL_FAILED"}}

    return {"status": "FAILED", "steps": max_steps, "model_usage": model_usage, "tool_usage": tool_usage,
            "error": {"code": "STEP_BUDGET_EXCEEDED"}}


__all__ = ["run_agent"]
