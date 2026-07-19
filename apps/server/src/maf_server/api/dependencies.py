"""Request dependencies shared by authenticated HTTP routers."""

from __future__ import annotations

from fastapi import Request

from maf_contracts.common import ActorContext
from maf_domain.errors import UnauthenticatedError


def _session_token(request: Request) -> str:
    """Read a session token from the standard bearer header or HttpOnly cookie."""

    authorization = request.headers.get("Authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return (request.cookies.get("maf_session") or "").strip()


async def get_current_actor(request: Request) -> ActorContext:
    """Authenticate the current request against the server-side session store."""

    token = _session_token(request)
    if not token:
        raise UnauthenticatedError("未认证")
    container = getattr(request.app.state, "container", None)
    iam_service = getattr(container, "services", {}).get("iam") if container else None
    authenticate = getattr(iam_service, "authenticate_session", None)
    if authenticate is None:
        raise UnauthenticatedError("未认证")
    return await authenticate(token)


async def get_current_actor_id(request: Request) -> str:
    """Authenticate the request and return its user id for legacy routers."""

    actor = await get_current_actor(request)
    return actor["user_id"]


__all__ = ["get_current_actor", "get_current_actor_id"]
