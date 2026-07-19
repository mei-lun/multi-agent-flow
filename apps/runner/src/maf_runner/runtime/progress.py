"""Coalesced, redacted progress event reporting over Git coordination."""

import inspect
import re
import time
from typing import Protocol


class ProgressReporter(Protocol):
    async def report(self, phase: str, percent: int | None, message: str, usage: dict) -> None:
        """脱敏并限频发送摘要；同 phase 高频更新合并，关键状态立即发送。"""
        ...
    async def flush(self) -> None:
        """作业结束前发送最后摘要；失败不阻止结果提交。"""
        ...


class BufferedProgressReporter:
    """Merge high-frequency updates and emit only durable milestones."""

    _SECRET = re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+")

    def __init__(self, event_sink, *, clock=time.monotonic, max_interval_seconds: int = 900) -> None:
        self._sink = event_sink
        self._clock = clock
        self._max_interval = max_interval_seconds
        self._pending: dict | None = None
        self._last_sent: dict | None = None
        self._last_sent_at = 0.0

    @classmethod
    def _redact(cls, value: str) -> str:
        text = cls._SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
        lowered = text.lower()
        if "chain of thought" in lowered or "internal reasoning" in lowered:
            return "[REDACTED_INTERNAL_REASONING]"
        return text[:2000]

    async def _emit(self, payload: dict) -> None:
        result = self._sink(dict(payload))
        if inspect.isawaitable(result):
            await result
        self._last_sent = dict(payload)
        self._last_sent_at = self._clock()

    async def report(
        self, phase: str, percent: int | None, message: str, usage: dict
    ) -> None:
        if percent is not None and (not isinstance(percent, int) or not 0 <= percent <= 100):
            raise ValueError("progress percent must be between 0 and 100")
        usage = dict(usage or {})
        payload = {
            "phase": phase,
            "percent": percent,
            "message": self._redact(str(message)),
            "completed_items": list(usage.get("completed_items", [])),
            "remaining_items": list(usage.get("remaining_items", [])),
            "problems": list(usage.get("problems", [])),
            "current_head_commit": usage.get("current_head_commit"),
            "test_summary": self._redact(str(usage.get("test_summary", ""))),
            "usage": {
                key: value for key, value in usage.items()
                if key in {"input_tokens", "output_tokens", "estimated_cost", "tool_calls"}
            },
        }
        self._pending = payload
        previous = self._last_sent
        percent_delta = (
            previous is None
            or percent is None
            or previous.get("percent") is None
            or abs(percent - int(previous["percent"])) >= 5
        )
        immediate = (
            previous is None
            or previous.get("phase") != phase
            or percent_delta
            or phase.upper() in {"BLOCKED", "FAILED", "SUBMITTED", "COMPLETED"}
            or self._clock() - self._last_sent_at >= self._max_interval
        )
        if immediate:
            await self._emit(payload)
            self._pending = None

    async def flush(self) -> None:
        if self._pending is None:
            return
        payload, self._pending = self._pending, None
        try:
            await self._emit(payload)
        except Exception:
            # Progress delivery is best effort; final submission must continue.
            return


__all__ = ["BufferedProgressReporter", "ProgressReporter"]
