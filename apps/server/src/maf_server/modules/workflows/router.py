"""Workflow 公共 HTTP 接口。"""

from typing import Protocol
from fastapi import APIRouter, Depends, Header, Request, status
from maf_contracts.common import ActorContext
from maf_server.api.dependencies import get_current_actor
from .schemas import *


class WorkflowHttpApi(Protocol):
    async def post_workflow(self, actor: ActorContext, request: CreateWorkflowRequest) -> WorkflowView:
        """POST `/api/v1/workflows`；创建成功 201。"""
        ...


def _actor(actor_id: str | None) -> ActorContext:
    return {"user_id": actor_id or "", "organization_id": "local", "permission_keys": [], "trace_id": ""}


async def _actor_dependency(
    request: Request,
    x_maf_actor_id: str | None = Header(default=None),
) -> ActorContext:
    if x_maf_actor_id:
        return _actor(x_maf_actor_id)
    return await get_current_actor(request)


def build_workflows_router(service: object) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["workflows"])

    @router.get("/workflows")
    async def list_workflows(
        actor: ActorContext = Depends(_actor_dependency),
    ) -> list[dict]:
        repository = getattr(service, "repository", None)
        workflows = getattr(repository, "workflows", {})
        return list(workflows.values())

    @router.post("/workflows", status_code=status.HTTP_201_CREATED)
    async def create_workflow(request: dict, actor: ActorContext = Depends(_actor_dependency)) -> dict:
        return await service.create_workflow(actor, request)

    @router.post("/workflows/{workflow_id}/versions", status_code=status.HTTP_201_CREATED)
    async def create_version(workflow_id: str, request: dict, actor: ActorContext = Depends(_actor_dependency)) -> dict:
        return await service.create_version(actor, workflow_id, request)

    @router.put("/workflow-versions/{version_id}/graph")
    async def save_graph(version_id: str, request: dict, actor: ActorContext = Depends(_actor_dependency)) -> dict:
        return await service.save_graph(actor, version_id, request)

    @router.post("/workflow-versions/{version_id}/validate")
    async def validate(version_id: str, actor: ActorContext = Depends(_actor_dependency)) -> dict:
        return await service.validate_version(actor, version_id)

    @router.post("/workflow-versions/{version_id}/publish")
    async def publish(version_id: str, request: dict, actor: ActorContext = Depends(_actor_dependency)) -> dict:
        return await service.publish(actor, version_id, request)

    @router.get("/workflow-versions/{version_id}/diff")
    async def diff(version_id: str, other: str, actor: ActorContext = Depends(_actor_dependency)) -> dict:
        return await service.diff(actor, version_id, other)

    return router
