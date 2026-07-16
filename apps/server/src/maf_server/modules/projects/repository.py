"""Project 模块持久化接口。"""

from typing import Protocol
from .schemas import *


class ProjectRepository(Protocol):
    async def get(self, project_id: str) -> ProjectView | None:
        """按 ID 读取项目当前版本；不存在为 None，权限在 Service 检查。"""
        ...
    async def list(self, query: ProjectQuery, visible_project_ids: set[str]) -> ProjectPage:
        """在 visible_project_ids 交集内过滤并游标分页。"""
        ...
    async def save(self, project: ProjectView, expected_version: int | None) -> ProjectView:
        """创建或乐观锁更新 Project；成功返回递增 version。"""
        ...
    async def append_input(self, item: ProjectInputView) -> ProjectInputView:
        """原子计算下一个项目输入版本；不得修改已有版本。"""
        ...
    async def save_repository_binding(
        self, binding: RepositoryBindingView
    ) -> RepositoryBindingView:
        """保存 UNVERIFIED/状态化绑定和 opaque credential reference，不保存凭据。"""
        ...
    async def save_change_request(self, item: ChangeRequestView) -> ChangeRequestView:
        """幂等追加变更请求；不能修改已决定请求的正文。"""
        ...
