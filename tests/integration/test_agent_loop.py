"""TASK-075 bounded agent loop tests."""

from __future__ import annotations

import asyncio

import pytest

from maf_runner.runtime.agent_loop import run_agent


class _Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def invoke(self, request):
        self.requests.append(request)
        return next(self.responses)


class _Tools:
    def __init__(self):
        self.calls = []

    async def list_allowed(self):
        return {"attempt_id": "a1", "tools": [{"key": "echo", "version": 1}]}

    async def call(self, key, request):
        self.calls.append((key, request))
        return {
            "call_id": "c1",
            "status": "COMPLETED",
            "output": {"value": request["arguments"]["value"]},
            "output_artifact_version_ids": [],
            "approval_inbox_item_id": None,
            "duration_ms": 0,
            "error": None,
        }


def _context(model, **extra):
    context = {
        "attempt_id": "a1",
        "model_client": model,
        "messages": [{"role": "user", "content": "return a value"}],
        "max_steps": 3,
        "max_tool_calls": 2,
        "timeout_seconds": 10,
        "budget": {"max_output_tokens": 20, "max_cost_amount": "1"},
    }
    context.update(extra)
    return context


@pytest.mark.asyncio
async def test_run_agent_completes_and_validates_contract():
    model = _Model(
        [
            {
                "status": "COMPLETED",
                "message": {"role": "assistant", "content": '{"value": 3}'},
                "tool_calls": [],
                "usage": {"input_tokens": 1, "output_tokens": 2, "estimated_cost": "0.01"},
            }
        ]
    )
    result = await run_agent(_context(model, output_contract={"type": "object", "required": ["value"]}))
    assert result["status"] == "COMPLETED"
    assert result["output"] == {"value": 3}
    assert result["model_usage"]["output_tokens"] == 2


@pytest.mark.asyncio
async def test_run_agent_rejects_tool_not_in_allowlist_without_calling_it():
    model = _Model(
        [
            {
                "status": "COMPLETED",
                "message": {"role": "assistant", "content": "use tool"},
                "tool_calls": [{"tool_key": "shell", "arguments": {}}],
                "usage": {},
            }
        ]
    )
    tools = _Tools()
    result = await run_agent(_context(model, tool_client=tools))
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "TOOL_NOT_AUTHORIZED"
    assert tools.calls == []


@pytest.mark.asyncio
async def test_run_agent_observes_tool_and_step_limits():
    model = _Model(
        [
            {
                "status": "COMPLETED",
                "message": {"role": "assistant", "content": "call"},
                "tool_calls": [{"tool_key": "echo", "arguments": {"value": "x"}}],
                "usage": {},
            }
        ]
    )
    tools = _Tools()
    result = await run_agent(_context(model, tool_client=tools, max_steps=1, max_tool_calls=10))
    assert result["status"] == "FAILED"
    assert result["error"]["code"] == "STEP_BUDGET_EXCEEDED"
    assert result["tool_usage"]["calls"] == 1


@pytest.mark.asyncio
async def test_run_agent_honours_cancellation_before_model_call():
    event = asyncio.Event()
    event.set()
    model = _Model([])
    result = await run_agent(_context(model, cancel_event=event))
    assert result["status"] == "CANCELLED"
    assert model.requests == []
