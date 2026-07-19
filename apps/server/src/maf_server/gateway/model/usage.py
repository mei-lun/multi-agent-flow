"""统一供应商 token、延迟、重试和估算费用的接口。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from maf_contracts.model import ModelUsage
from maf_domain.usage import money


class UsageRecorder(Protocol):
    async def reserve(self, run_id: str, attempt_id: str, estimated_cost: str, tokens: int, call_key: str | None = None) -> str:
        """原子检查剩余预算并返回 reservation_id；不足时拒绝且不调用模型。"""
        ...

    async def commit(self, reservation_id: str, actual: ModelUsage) -> None:
        """用实际 usage 结算并写 cost ledger；重复提交必须幂等。"""
        ...

    async def release(self, reservation_id: str, reason: str) -> None:
        """调用未发生时释放预留；保留失败审计。"""
        ...


@dataclass
class _Reservation:
    reservation_id: str
    run_id: str
    call_identity: str
    estimated_cost: Decimal
    tokens: int
    status: str = "RESERVED"
    actual: ModelUsage | None = None


class InMemoryUsageRecorder:
    """Atomic, idempotent budget ledger suitable for a node-local gateway."""

    def __init__(self, budgets: dict[str, str]) -> None:
        self._limits = {run_id: money(value) for run_id, value in budgets.items()}
        self._reservations: dict[str, _Reservation] = {}
        self._by_call: dict[tuple[str, str], str] = {}

    async def reserve(
        self,
        run_id: str,
        attempt_id: str,
        estimated_cost: str,
        tokens: int,
        call_key: str | None = None,
    ) -> str:
        estimate = money(estimated_cost)
        identity = call_key or attempt_id
        dedupe_key = (run_id, identity)
        existing = self._by_call.get(dedupe_key)
        if existing is not None:
            prior = self._reservations.get(existing)
            if prior is not None and prior.status != "RELEASED":
                return existing
            self._by_call.pop(dedupe_key, None)
        if run_id not in self._limits:
            raise ValueError("run budget is not configured")
        committed = sum(
            money(item.actual["estimated_cost"])
            for item in self._reservations.values()
            if item.run_id == run_id and item.status == "COMMITTED" and item.actual
        )
        reserved = sum(
            item.estimated_cost
            for item in self._reservations.values()
            if item.run_id == run_id and item.status == "RESERVED"
        )
        if committed + reserved + estimate > self._limits[run_id]:
            raise ValueError("budget exceeded")
        reservation_id = f"reservation-{uuid.uuid4()}"
        self._reservations[reservation_id] = _Reservation(
            reservation_id, run_id, identity, estimate, tokens
        )
        self._by_call[dedupe_key] = reservation_id
        return reservation_id

    async def commit(self, reservation_id: str, actual: ModelUsage) -> None:
        item = self._reservations[reservation_id]
        if item.status == "COMMITTED":
            return
        if item.status != "RESERVED":
            raise ValueError("reservation is not active")
        actual_cost = money(actual["estimated_cost"])
        other = sum(
            money(res.actual["estimated_cost"])
            for res in self._reservations.values()
            if res.run_id == item.run_id and res.status == "COMMITTED" and res.actual
        )
        if other + actual_cost > self._limits[item.run_id]:
            raise ValueError("actual usage exceeds budget")
        item.actual = dict(actual)
        item.status = "COMMITTED"

    async def release(self, reservation_id: str, reason: str) -> None:
        item = self._reservations[reservation_id]
        if item.status == "COMMITTED":
            return
        item.status = "RELEASED"

    def summary(self, run_id: str) -> dict[str, object]:
        committed_items = [
            item for item in self._reservations.values()
            if item.run_id == run_id and item.status == "COMMITTED" and item.actual
        ]
        cost = sum((money(item.actual["estimated_cost"]) for item in committed_items), Decimal("0"))
        return {
            "run_id": run_id,
            "cost": format(cost, "f"),
            "input_tokens": sum(item.actual["input_tokens"] for item in committed_items),
            "output_tokens": sum(item.actual["output_tokens"] for item in committed_items),
            "currency": committed_items[0].actual["currency"] if committed_items else "USD",
        }


__all__ = ["InMemoryUsageRecorder", "UsageRecorder"]
