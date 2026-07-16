"""Project 应用服务接口。"""

from typing import Protocol

from maf_contracts.common import ActorContext
from .schemas import *


class ProjectApplicationService(Protocol):
    async def create_project(
        self, actor: ActorContext, request: CreateProjectRequest
    ) -> ProjectView:
        """创建项目并返回版本 1。

        顺序：检查创建权限；校验 owner、默认 Workflow Version、金额和运行时上限；检查
        幂等键；保存 Project；写 `project.created` 事件。不会自动启动 Run 或访问仓库。
        """
        ...

    async def list_projects(self, actor: ActorContext, query: ProjectQuery) -> ProjectPage:
        """只返回调用者有查看权限的项目，使用稳定排序与游标分页。"""
        ...

    async def get_project(self, actor: ActorContext, project_id: str) -> ProjectView:
        """检查项目成员权限后返回项目；不存在和不可见应分别映射 404/403。"""
        ...

    async def update_project(
        self, actor: ActorContext, project_id: str, request: UpdateProjectRequest
    ) -> ProjectView:
        """按 expected_version 更新项目。

        已开始的 Run 使用自己的快照，不随项目默认配置变化。归档项目不得再启动新 Run，
        但历史数据仍可读。版本冲突返回 409。
        """
        ...

    async def add_input_version(
        self, actor: ActorContext, project_id: str, request: AddProjectInputRequest
    ) -> ProjectInputView:
        """为项目追加不可变输入版本。

        先确认上传 Artifact 完整且调用者可读，再生成递增版本。不能覆盖旧版本；相同幂等键
        返回同一版本。输出供启动 Run 时固定引用。
        """
        ...

    async def bind_repository(
        self, actor: ActorContext, project_id: str, request: BindRepositoryRequest
    ) -> RepositoryBindingView:
        """登记 GitHub 或本地 Git 绑定，但不宣称连接已经可用。

        校验路径/URL形式和 Secret 引用，保存状态 UNVERIFIED；真实访问由 Repository
        Gateway 的 verify 接口完成。不得把凭据复制进绑定记录。
        """
        ...

    async def create_change_request(
        self, actor: ActorContext, project_id: str, request: CreateChangeRequest
    ) -> ChangeRequestView:
        """记录运行中需求变更并触发站内审批。

        检查 Run 属于项目且未结束；保存请求与受影响需求；创建 Inbox Item，并通过事件让
        Scheduler 决定暂停或后续重规划。此接口本身不直接修改正在执行的 Graph State。
        """
        ...

