"""Token, time, retry, and budget value objects."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def money(value: str) -> Decimal:
    """Parse a non-negative decimal money string without using binary floats."""
    if not isinstance(value, str):
        raise ValueError("money must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal money string") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("money must be finite and non-negative")
    return parsed


@dataclass(frozen=True)
class BudgetUsage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost: str
    currency: str = "USD"
