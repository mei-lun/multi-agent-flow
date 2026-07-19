"""Compose implemented public business routers under ``/api/v1``.

Router factories remain owned by their feature modules.  This module only
decides which already-assembled services are exposed, making it safe to use a
partial container during development and in focused tests.
"""

from __future__ import annotations

from typing import Any
from fastapi import APIRouter


def build_router(container: Any | None = None, *, services: dict[str, Any] | None = None):
    """Build the public API router for a container or service mapping.

    Missing services are skipped.  This is intentional while scheduler/run,
    artifact, role, and workflow modules are still being completed; the
    aggregator must not invent placeholder endpoints that imply functionality
    which is not available.
    """

    if services is None:
        services = getattr(container, "services", None) or {}

    router = APIRouter()

    def include(
        factory_module: str,
        factory_name: str,
        service_name: str,
        *extra: Any,
        **options: Any,
    ) -> None:
        service = services.get(service_name)
        if service is None:
            return
        module = __import__(factory_module, fromlist=[factory_name])
        factory = getattr(module, factory_name)
        router.include_router(factory(service, *extra, **options))

    # Keep this order stable for generated OpenAPI documents and predictable
    # route matching (notably the project/repository paths).
    include("maf_server.modules.iam.router", "build_auth_router", "iam", secure_cookie=False)
    include("maf_server.modules.iam.router", "build_current_user_router", "iam")
    include("maf_server.modules.iam.router", "build_settings_router", "iam")
    include("maf_server.modules.projects.router", "build_projects_router", "projects")
    include(
        "maf_server.modules.repositories.router",
        "build_repositories_router",
        "repositories",
    )
    include(
        "maf_server.modules.model_connections.router",
        "build_model_connection_router",
        "model_connections",
    )
    include("maf_server.modules.tools.router", "build_tools_router", "tools")
    include(
        "maf_server.modules.tools.router",
        "build_mcp_sync_router",
        "tools",
    )
    include(
        "maf_server.modules.reviews.router",
        "build_review_router",
        "reviews",
        services.get("quality_gates"),
    )
    include("maf_server.modules.inbox.router", "build_inbox_router", "inbox")
    include("maf_server.modules.workflows.router", "build_workflows_router", "workflows")
    from maf_server.api.v1.ui_support import build_ui_support_router

    router.include_router(build_ui_support_router())
    return router


# A module-level router is useful for consumers that only need a stable import
# and does not touch the database.  ``create_app`` uses ``build_router`` so its
# concrete dependencies are present.
router = APIRouter()


__all__ = ["build_router", "router"]
