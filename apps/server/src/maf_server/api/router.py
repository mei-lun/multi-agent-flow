"""Top-level public Web API router registration.

The server intentionally has no ``/internal/v1`` HTTP API.  Nodes coordinate
through protected Git refs and ``.maf`` files, while user-facing endpoints are
mounted below ``/api/v1``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from maf_server.api.v1.router import build_router as build_v1_router


def build_router(
    container: Any | None = None,
    *,
    services: dict[str, Any] | None = None,
) -> APIRouter:
    """Return the complete public API router for the supplied composition."""

    router = APIRouter()
    router.include_router(build_v1_router(container, services=services))
    return router


# Import-safe empty router for applications that build their own composition.
router = APIRouter()


__all__ = ["build_router", "router"]
