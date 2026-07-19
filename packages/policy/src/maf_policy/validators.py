"""Side-effect-free validators used at the execution capability boundary."""

from __future__ import annotations

import ipaddress
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def validate_path(path: str, allowed_roots: list[str]) -> bool:
    try:
        candidate = Path(path).resolve()
    except (OSError, ValueError, TypeError):
        return False
    for root in allowed_roots:
        try:
            candidate.relative_to(Path(root).resolve())
            return True
        except (OSError, ValueError, TypeError):
            continue
    return False


def validate_url(url: str, allowed_hosts: list[str], *, allow_private: bool = False) -> bool:
    try:
        parsed = urlparse(url)
        host = parsed.hostname
    except (TypeError, ValueError):
        return False
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    if host.casefold() not in {item.casefold() for item in allowed_hosts}:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return allow_private or not (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_multicast or address.is_reserved
    )


def validate_budget(requested: str, maximum: str) -> bool:
    try:
        value = Decimal(requested)
        limit = Decimal(maximum)
    except (InvalidOperation, TypeError):
        return False
    return value.is_finite() and limit.is_finite() and Decimal("0") <= value <= limit


def validate_parameter_constraints(parameters: dict[str, Any], allowed: dict[str, Any]) -> bool:
    for key, expected in allowed.items():
        if key not in parameters:
            continue
        value = parameters[key]
        if isinstance(expected, list) and value not in expected:
            return False
        if isinstance(expected, dict):
            if "min" in expected and (not isinstance(value, (int, float)) or value < expected["min"]):
                return False
            if "max" in expected and (not isinstance(value, (int, float)) or value > expected["max"]):
                return False
    return True


__all__ = ["validate_budget", "validate_parameter_constraints", "validate_path", "validate_url"]
