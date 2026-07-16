"""Workflow 公共 HTTP 接口。"""

from typing import Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class WorkflowHttpApi(Protocol):
    async def post_workflow(self, actor: ActorContext, request: CreateWorkflowRequest) -> WorkflowView:
        """POST `/api/v1/workflows`；创建成功 201。"""
        ...
    async def post_version(self, actor: ActorContext, workflow_id: str, request: CreateWorkflowVersionRequest) -> WorkflowVersionView:
        """POST `/api/v1/workflows/{id}/versions`；创建 DRAFT，成功 201。"""
        ...
    async def put_graph(self, actor: ActorContext, version_id: str, request: SaveGraphRequest) -> WorkflowVersionView:
        """PUT `/api/v1/workflow-versions/{id}/graph`；完整替换草稿图。"""
        ...
    async def post_validate(self, actor: ActorContext, version_id: str) -> ValidationReport:
        """POST `/api/v1/workflow-versions/{id}/validate`；返回全部错误和警告。"""
        ...
    async def post_publish(self, actor: ActorContext, version_id: str, request: PublishWorkflowRequest) -> WorkflowVersionView:
        """POST `/api/v1/workflow-versions/{id}/publish`；成功 200，校验失败 422。"""
        ...
    async def get_diff(self, actor: ActorContext, version_id: str, other: str) -> WorkflowDiff:
        """GET `/api/v1/workflow-versions/{id}/diff?other=`；成功 200。"""
        ...

