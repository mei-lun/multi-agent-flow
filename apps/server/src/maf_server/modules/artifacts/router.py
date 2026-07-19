"""HTTP routes for artifact metadata, schema, lineage and diff operations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Header, Query


def _actor(
    actor_id: str | None, organization_id: str | None, trace_id: str | None
) -> dict[str, Any]:
    return {
        "user_id": actor_id or "anonymous",
        "organization_id": organization_id or "system",
        "permission_keys": ["ADMIN"] if actor_id else [],
        "trace_id": trace_id or "",
    }


def build_artifact_router(artifact_service: Any, schema_service: Any | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/artifacts")
    async def list_artifacts(
        project_id: str = Query(...),
        actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
        organization_id: str | None = Header(default=None, alias="X-Organization-Id"),
        trace_id: str | None = Header(default=None, alias="X-Trace-Id"),
    ):
        actor = _actor(actor_id, organization_id, trace_id)
        return await artifact_service.list_artifacts(
            project_id, actor_id=actor["user_id"], actor=actor
        )

    @router.get("/artifacts/{artifact_id}")
    async def get_artifact(
        artifact_id: str,
        actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
    ):
        actor = _actor(actor_id, None, None)
        return await artifact_service.get_artifact(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )

    if schema_service is None:
        return router

    @router.post("/artifact-schemas")
    async def register_schema(
        payload: dict[str, Any] = Body(...),
        actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
    ):
        actor = _actor(actor_id, None, None)
        return await schema_service.register_schema(
            payload["schema_name"],
            payload["version"],
            payload["json_schema"],
            actor_id=actor["user_id"],
            actor=actor,
        )

    @router.get("/artifact-schemas")
    async def list_schemas(
        schema_name: str | None = Query(default=None),
        actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
    ):
        actor = _actor(actor_id, None, None)
        return await schema_service.list_schemas(
            schema_name=schema_name, actor_id=actor["user_id"], actor=actor
        )

    @router.get("/artifact-schemas/{schema_name}/{version}")
    async def get_schema(
        schema_name: str,
        version: int,
        actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
    ):
        actor = _actor(actor_id, None, None)
        return await schema_service.get_schema(
            schema_name, version, actor_id=actor["user_id"], actor=actor
        )

    @router.post("/artifact-schemas/{schema_name}/{version}/deprecate")
    async def deprecate_schema(
        schema_name: str,
        version: int,
        actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
    ):
        actor = _actor(actor_id, None, None)
        return await schema_service.deprecate_schema(
            schema_name, version, actor_id=actor["user_id"], actor=actor
        )

    @router.post("/artifacts/{artifact_id}/validate")
    async def validate_artifact(
        artifact_id: str,
        schema_name: str,
        schema_version: int,
        actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
    ):
        actor = _actor(actor_id, None, None)
        return await schema_service.validate_artifact(
            artifact_id, schema_name, schema_version, actor_id=actor["user_id"], actor=actor
        )

    @router.get("/artifacts/{artifact_id}/lineage")
    async def get_lineage(
        artifact_id: str, actor_id: str | None = Header(default=None, alias="X-Actor-Id")
    ):
        actor = _actor(actor_id, None, None)
        return await schema_service.get_lineage(artifact_id, actor_id=actor["user_id"], actor=actor)

    @router.get("/artifacts/{artifact_id}/upstream")
    async def get_upstream(
        artifact_id: str, actor_id: str | None = Header(default=None, alias="X-Actor-Id")
    ):
        actor = _actor(actor_id, None, None)
        return await schema_service.get_upstream(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )

    @router.get("/artifacts/{artifact_id}/downstream")
    async def get_downstream(
        artifact_id: str, actor_id: str | None = Header(default=None, alias="X-Actor-Id")
    ):
        actor = _actor(actor_id, None, None)
        return await schema_service.get_downstream(
            artifact_id, actor_id=actor["user_id"], actor=actor
        )

    @router.get("/artifacts/{artifact_id}/diff")
    async def diff_artifacts(
        artifact_id: str,
        other_artifact_id: str,
        actor_id: str | None = Header(default=None, alias="X-Actor-Id"),
    ):
        actor = _actor(actor_id, None, None)
        return await schema_service.diff_artifacts(
            artifact_id, other_artifact_id, actor_id=actor["user_id"], actor=actor
        )

    return router


__all__ = ["build_artifact_router"]
