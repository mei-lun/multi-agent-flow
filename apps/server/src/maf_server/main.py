"""FastAPI application entry point.

Responsibilities:
- create the HTTP application;
- install public and internal routers;
- start scheduler, outbox, lease-reaper, and cleanup lifecycles;
- serve the built web application in packaged deployments.
"""

from __future__ import annotations


def create_app():
    """Build the application after dependencies are assembled in bootstrap.py."""
    raise NotImplementedError("Application assembly is implemented in the next phase")

