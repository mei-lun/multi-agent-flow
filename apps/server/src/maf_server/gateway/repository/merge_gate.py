"""Deterministic final merge gate shared by local and GitHub review adapters."""

from __future__ import annotations

from typing import Any


class MergeGate:
    """Require every persisted gate and an unchanged expected head before merge."""

    REQUIRED = ("code_review", "tests", "product_acceptance", "inbox", "checks")

    @classmethod
    def blocking_reasons(cls, review: dict[str, Any], *, expected_head: str) -> list[str]:
        reasons: list[str] = []
        if review.get("head_commit") != expected_head:
            reasons.append("HEAD_CHANGED")
        for name in cls.REQUIRED:
            value = review.get(name)
            if isinstance(value, dict):
                value = value.get("status") or value.get("decision")
            if value not in {"PASS", "APPROVED", "APPROVE", True}:
                reasons.append(f"{name.upper()}_NOT_PASSED")
        if review.get("changes_requested"):
            reasons.append("CHANGES_REQUESTED")
        if review.get("mergeable") is False:
            reasons.append("NOT_MERGEABLE")
        return reasons

    async def merge(self, adapter, command: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
        expected = str(command.get("expected_head_commit") or "")
        reasons = self.blocking_reasons(review, expected_head=expected)
        if reasons:
            status = "CONFLICTED" if "HEAD_CHANGED" in reasons else "FAILED"
            return {"status": status, "merge_commit": None, "message": ";".join(reasons), "reasons": reasons}
        result = await adapter.merge_review(command)
        if result.get("status") == "MERGED":
            result["control_task_status"] = "DONE"
        return result


__all__ = ["MergeGate"]
