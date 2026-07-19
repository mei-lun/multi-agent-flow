"""Run 公共 HTTP 与 SSE 接口。"""

from typing import AsyncIterator, Protocol
from fastapi import APIRouter, Header, status
from maf_contracts.common import ActorContext
from .schemas import *


class RunHttpApi(Protocol):
    async def post_run(self, actor: ActorContext, project_id: str, request: StartRunRequest) -> RunView:
        """POST `/api/v1/projects/{id}/runs`；创建成功 201。"""
        ...
    async def get_run(self, actor: ActorContext, run_id: str) -> RunView:
        """GET `/api/v1/runs/{id}`；返回 Run 投影。"""
        ...
    async def get_graph(self, actor: ActorContext, run_id: str) -> RunGraphView:
        """GET `/api/v1/runs/{id}/graph`；返回节点状态图。"""
        ...
    async def get_tasks(self, actor: ActorContext, run_id: str, query: TaskQuery) -> TaskPage:
        """GET `/api/v1/runs/{id}/tasks`；分页返回 Task/Attempt。"""
        ...
    async def get_events(self, actor: ActorContext, run_id: str, last_event_id: str | None) -> AsyncIterator[RunEventView]:
        """GET `/api/v1/runs/{id}/events`；SSE，可通过 Last-Event-ID 续传。"""
        ...
    async def post_pause(self, actor: ActorContext, run_id: str, request: RunCommand) -> CommandResult:
        """POST `/api/v1/runs/{id}:pause`；受理返回 202。"""
        ...
    async def post_resume(self, actor: ActorContext, run_id: str, request: ResumeRunRequest) -> CommandResult:
        """POST `/api/v1/runs/{id}:resume`；从 checkpoint 恢复，返回 202。"""
        ...
    async def post_cancel(self, actor: ActorContext, run_id: str, request: RunCommand) -> CommandResult:
        """POST `/api/v1/runs/{id}:cancel`；幂等受理返回 202。"""
        ...
    async def post_increase_budget(self, actor: ActorContext, run_id: str, request: IncreaseBudgetRequest) -> RunView:
        """POST `/api/v1/runs/{id}:increase-budget`；成功 200。"""
        ...
    async def post_retry_task(self, actor: ActorContext, task_id: str, request: RetryTaskRequest) -> TaskView:
        """POST `/api/v1/tasks/{id}:retry`；新 Attempt 创建成功 202。"""
        ...


def _actor(actor_id: str | None) -> ActorContext:
    return {"user_id": actor_id or "", "organization_id": "local", "permission_keys": [], "trace_id": ""}


def build_runs_router(service: object) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["runs"])

    @router.post("/projects/{project_id}/runs", status_code=status.HTTP_201_CREATED)
    async def start(project_id: str, request: dict, x_maf_actor_id: str | None = Header(default=None)) -> dict:
        return await service.start_run(_actor(x_maf_actor_id), project_id, request)

    @router.get("/runs/{run_id}")
    async def get(run_id: str, x_maf_actor_id: str | None = Header(default=None)) -> dict:
        return await service.get_run(_actor(x_maf_actor_id), run_id)

    @router.get("/runs/{run_id}/graph")
    async def graph(run_id: str, x_maf_actor_id: str | None = Header(default=None)) -> dict:
        return await service.get_graph(_actor(x_maf_actor_id), run_id)

    @router.post("/runs/{run_id}:pause", status_code=status.HTTP_202_ACCEPTED)
    async def pause(run_id: str, request: dict, x_maf_actor_id: str | None = Header(default=None)) -> dict:
        return await service.pause(_actor(x_maf_actor_id), run_id, request)

    @router.post("/runs/{run_id}:resume", status_code=status.HTTP_202_ACCEPTED)
    async def resume(run_id: str, request: dict, x_maf_actor_id: str | None = Header(default=None)) -> dict:
        return await service.resume(_actor(x_maf_actor_id), run_id, request)

    @router.post("/runs/{run_id}:cancel", status_code=status.HTTP_202_ACCEPTED)
    async def cancel(run_id: str, request: dict, x_maf_actor_id: str | None = Header(default=None)) -> dict:
        return await service.cancel(_actor(x_maf_actor_id), run_id, request)

    return router
