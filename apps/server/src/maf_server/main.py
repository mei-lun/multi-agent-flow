"""FastAPI application entry point.

Only application lifecycle and transport concerns live here.  Business
dependencies are assembled by :func:`maf_server.bootstrap.build_container` and
are not opened during module import.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from maf_server.api.errors import register_error_handlers
from maf_server.api.router import build_router
from maf_server.bootstrap import ServerContainer, build_container


def create_app(
    settings: Any | None = None,
    *,
    container: ServerContainer | None = None,
):
    """Construct the FastAPI application.

    ``settings`` and ``container`` are optional injection points for tests and
    embedding.  If neither is supplied, settings are loaded from ``MAF_*``
    environment variables when this function is called (never at import
    time).
    """

    from fastapi import FastAPI

    if container is None:
        # Keep the no-argument path friendly to tests that monkeypatch the
        # composition function with a zero-argument factory.
        container = build_container() if settings is None else build_container(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        startup = getattr(container, "startup", None)
        if startup is not None:
            result = startup()
            if hasattr(result, "__await__"):
                await result
        try:
            yield
        finally:
            close = getattr(container, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result

    app = FastAPI(
        title="Multi Agent Flow Server",
        version="0.0.0",
        lifespan=lifespan,
    )
    app.state.container = container
    register_error_handlers(app)
    app.include_router(build_router(container))

    @app.get("/health", tags=["system"])
    @app.get("/healthz", tags=["system"], include_in_schema=False)
    async def health() -> dict[str, Any]:
        """Return a lightweight process/storage health response."""

        database = getattr(container, "database", None)
        initialized = bool(getattr(database, "is_initialized", False))
        closed = bool(getattr(database, "is_closed", False))
        return {
            "status": "ok" if initialized and not closed else "starting",
            "database": "ready" if initialized and not closed else "not_ready",
        }

    return app


# Uvicorn's conventional import target: ``uvicorn maf_server.main:app``.
# Creating the app is intentionally deferred to the process launcher; this
# avoids loading required environment settings when a module is merely imported.
app = None


__all__ = ["app", "create_app"]
