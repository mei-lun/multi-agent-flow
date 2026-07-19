"""Small control-plane read endpoints used by the initial Web console shell."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from maf_contracts.common import ActorContext
from maf_server.api.dependencies import get_current_actor


def build_ui_support_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["console"])

    @router.get("/skills")
    async def list_skills(actor: ActorContext = Depends(get_current_actor)) -> list[Any]:
        return []

    @router.get("/roles")
    async def list_roles(actor: ActorContext = Depends(get_current_actor)) -> list[Any]:
        return []

    @router.get("/settings")
    async def list_settings(actor: ActorContext = Depends(get_current_actor)) -> dict[str, Any]:
        return {}

    @router.get("/nodes")
    async def list_nodes(actor: ActorContext = Depends(get_current_actor)) -> list[Any]:
        return []

    @router.get("/audit")
    async def list_audit(actor: ActorContext = Depends(get_current_actor)) -> list[Any]:
        return []

    @router.get("/git/projector")
    async def projector_status(actor: ActorContext = Depends(get_current_actor)) -> dict[str, Any]:
        return {"status": "READY", "projected_control_commit": None}

    @router.get("/model-usage")
    async def model_usage(actor: ActorContext = Depends(get_current_actor)) -> list[Any]:
        return []

    @router.post("/policies/simulate")
    async def simulate_policy(payload: dict[str, Any], actor: ActorContext = Depends(get_current_actor)) -> dict[str, Any]:
        return {"allowed": "ADMIN" in actor.get("permission_keys", []), "reason": "ADMIN_DEFAULT_ALLOW"}

    return router


__all__ = ["build_ui_support_router"]
