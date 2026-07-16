"""Composition root for server dependencies.

Only this module may wire concrete repositories, adapters, scheduler services,
and background workers to their Protocol interfaces.
"""

from __future__ import annotations


class ServerContainer:
    """Typed holder for long-lived server dependencies."""


def build_container():
    """Create database, stores, adapters, gateways, and application services."""
    raise NotImplementedError

