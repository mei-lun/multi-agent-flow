"""Project 公共 HTTP 接口。"""

from typing import Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class ProjectHttpApi(Protocol):
    async def post_project(self, actor: ActorContext, request: CreateProjectRequest) -> ProjectView:
        """POST `/api/v1/projects`；创建成功 201。"""
        ...
    async def get_projects(self, actor: ActorContext, query: ProjectQuery) -> ProjectPage:
        """GET `/api/v1/projects`；返回调用者可见项目。"""
        ...
    async def get_project(self, actor: ActorContext, project_id: str) -> ProjectView:
        """GET `/api/v1/projects/{id}`；成功 200。"""
        ...
    async def patch_project(
        self, actor: ActorContext, project_id: str, request: UpdateProjectRequest
    ) -> ProjectView:
        """PATCH `/api/v1/projects/{id}`；乐观锁更新。"""
        ...
    async def post_input(
        self, actor: ActorContext, project_id: str, request: AddProjectInputRequest
    ) -> ProjectInputView:
        """POST `/api/v1/projects/{id}/inputs`；追加不可变输入版本，成功 201。"""
        ...
    async def post_repository(
        self, actor: ActorContext, project_id: str, request: BindRepositoryRequest
    ) -> RepositoryBindingView:
        """POST `/api/v1/projects/{id}/repositories`；创建未验证绑定，成功 201。"""
        ...
    async def post_change_request(
        self, actor: ActorContext, project_id: str, request: CreateChangeRequest
    ) -> ChangeRequestView:
        """POST `/api/v1/projects/{id}/change-requests`；受理后返回 202。"""
        ...

