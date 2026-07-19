import pytest

from maf_runner.runtime.progress import BufferedProgressReporter


@pytest.mark.asyncio
async def test_progress_is_coalesced_redacted_and_contains_structured_summary():
    events = []
    clock = [0.0]
    reporter = BufferedProgressReporter(events.append, clock=lambda: clock[0])
    await reporter.report("RUNNING", 1, "token=secret", {"completed_items": ["a"], "remaining_items": ["b"], "problems": [], "current_head_commit": "abc", "test_summary": "ok"})
    await reporter.report("RUNNING", 2, "small increment", {})
    assert len(events) == 1
    assert "secret" not in events[0]["message"]
    await reporter.flush()
    assert len(events) == 2
    assert events[0]["completed_items"] == ["a"]
    assert events[0]["current_head_commit"] == "abc"


@pytest.mark.asyncio
async def test_blocked_progress_is_immediate_and_reasoning_is_removed():
    events = []
    reporter = BufferedProgressReporter(events.append)
    await reporter.report("RUNNING", 10, "ok", {})
    await reporter.report("BLOCKED", 10, "internal reasoning: do not expose", {"problems": [{"code": "WAIT"}]})
    assert len(events) == 2
    assert events[-1]["message"] == "[REDACTED_INTERNAL_REASONING]"
