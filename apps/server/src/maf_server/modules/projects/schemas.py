"""Project 模块的请求、响应和查询契约。

TASK-033 范围：
- 定义项目 CRUD 与成员管理所需的 TypedDict：``ProjectView``、``ProjectMemberView``、
  ``CreateProjectRequest``、``UpdateProjectRequest``、``AddMemberRequest``、
  ``UpdateMemberRoleRequest``、``ProjectListQuery``、``ProjectPage``。
- 保留后续任务（034/035/036）所需的 TypedDict（如 ``AddProjectInputRequest``、
  ``BindRepositoryRequest``、``CreateChangeRequest``），这些定义在本任务中不被使用，
  仅作为接口契约占位，避免后续任务重复定义。

字段对齐 ``migrations/0001_projects_and_members.sql``：
- ``projects``：id/name/description/status/created_at/created_by/updated_at/version_no/deleted_at
- ``project_members``：project_id/user_id/role/added_at/added_by/version_no
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from maf_contracts.common import Money

# --------------------------------------------------------------------------- #
# 角色与状态枚举（与迁移脚本 CHECK 约束一致）
# --------------------------------------------------------------------------- #

#: 项目成员角色枚举（与 ``project_members.role`` CHECK 约束一致）。
ProjectMemberRole = Literal["OWNER", "APPROVER", "OBSERVER", "DESIGNER"]

#: 项目状态枚举（与 ``projects.status`` CHECK 约束一致）。
ProjectStatus = Literal["ACTIVE", "ARCHIVED"]


# --------------------------------------------------------------------------- #
# TASK-033 视图与请求
# --------------------------------------------------------------------------- #


class ProjectView(TypedDict):
    """项目当前快照视图，对齐 ``projects`` 表字段。

    ``deleted_at`` 在未软删除时为 ``None``；查询接口仅返回未软删除项目。
    """

    id: str
    name: str
    description: str
    status: ProjectStatus
    created_at: str
    created_by: str
    updated_at: str
    version: int
    deleted_at: str | None


class ProjectMemberView(TypedDict):
    """项目成员视图，对齐 ``project_members`` 表字段。"""

    project_id: str
    user_id: str
    role: ProjectMemberRole
    added_at: str
    added_by: str
    version: int


class CreateProjectRequest(TypedDict):
    """``create_project`` 请求体。``actor_id`` 由调用方关键字传入，不在本结构中。"""

    name: str
    description: str


class UpdateProjectRequest(TypedDict, total=False):
    """``update_project`` 请求体；``expected_version`` 必填用于乐观锁。"""

    name: str
    description: str
    status: ProjectStatus
    expected_version: int


class AddMemberRequest(TypedDict):
    """``add_member`` 请求体。"""

    user_id: str
    role: ProjectMemberRole


class UpdateMemberRoleRequest(TypedDict):
    """``update_member_role`` 请求体。"""

    new_role: ProjectMemberRole


class ProjectListQuery(TypedDict, total=False):
    """``list_projects`` 查询参数（保留扩展位，TASK-033 仅返回可见项目）。"""

    limit: int


class ProjectPage(TypedDict):
    """``list_projects`` 分页响应。TASK-033 不分页，``next_cursor`` 恒为 ``None``。"""

    items: list[ProjectView]
    next_cursor: str | None
    has_more: bool


# --------------------------------------------------------------------------- #
# 后续任务（034/035/036）契约占位
#
# 这些 TypedDict 仅为接口契约占位，TASK-033 不实现对应方法；保留定义避免后续
# 任务重复定义造成字段漂移。``ProjectFutureView`` 包含未来扩展字段（预算、
# 默认 Workflow 等），与 ``ProjectView`` 分开以保持 TASK-033 视图简洁。
# --------------------------------------------------------------------------- #


class ProjectFutureView(TypedDict):
    """后续任务扩展的项目视图（含预算、默认 Workflow 等），TASK-033 不使用。"""

    id: str
    name: str
    description: str
    owner_user_id: str
    data_classification: Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL"]
    default_workflow_version_id: str | None
    budget: Money
    max_run_seconds: int
    status: ProjectStatus
    version: int
    created_at: str


class CreateProjectFutureRequest(TypedDict):
    """后续任务扩展的创建请求，TASK-033 不使用。"""

    name: str
    description: str
    owner_user_id: str
    data_classification: Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL"]
    default_workflow_version_id: str | None
    budget: Money
    max_run_seconds: int
    idempotency_key: str


class UpdateProjectFutureRequest(TypedDict, total=False):
    """后续任务扩展的更新请求，TASK-033 不使用。"""

    name: str
    description: str
    default_workflow_version_id: str | None
    budget: Money
    max_run_seconds: int
    status: ProjectStatus
    expected_version: int
    idempotency_key: str


class ProjectFutureQuery(TypedDict, total=False):
    """后续任务扩展的查询参数，TASK-033 不使用。"""

    cursor: str
    limit: int
    owner_user_id: str
    status: ProjectStatus
    keyword: str


class AddProjectInputRequest(TypedDict):
    """``add_input_version`` 请求体（TASK-034），TASK-033 不实现。"""

    name: str
    content_type: str
    upload_artifact_version_id: str
    change_summary: str
    idempotency_key: str


class ProjectInputView(TypedDict):
    """项目输入版本视图（TASK-034），TASK-033 不实现。"""

    id: str
    project_id: str
    version: int
    name: str
    content_type: str
    artifact_version_id: str
    change_summary: str
    created_at: str


class BindRepositoryRequest(TypedDict):
    """``bind_repository`` 请求体（TASK-035），TASK-033 不实现。"""

    repository_type: Literal["GITHUB", "LOCAL_GIT"]
    display_name: str
    location: str
    base_branch: str
    credential_secret_id: NotRequired[str]
    idempotency_key: str


class RepositoryBindingView(TypedDict):
    """仓库绑定视图（TASK-035），TASK-033 不实现。"""

    id: str
    project_id: str
    repository_type: Literal["GITHUB", "LOCAL_GIT"]
    display_name: str
    location: str
    base_branch: str
    credential_configured: bool
    verified_commit: str | None
    status: Literal["UNVERIFIED", "READY", "ERROR"]
    version: int


class CreateChangeRequest(TypedDict):
    """``create_change_request`` 请求体（TASK-036），TASK-033 不实现。"""

    run_id: str
    title: str
    description: str
    affected_requirement_ids: list[str]
    requested_action: Literal["PAUSE_AND_REPLAN", "APPLY_NEXT_NODE", "CANCEL"]
    idempotency_key: str


class ChangeRequestView(TypedDict):
    """变更请求视图（TASK-036），TASK-033 不实现。"""

    id: str
    project_id: str
    run_id: str
    status: Literal["PENDING", "APPROVED", "REJECTED", "APPLIED"]
    title: str
    description: str
    affected_requirement_ids: list[str]
    created_at: str


__all__ = [
    # TASK-033
    "ProjectMemberRole",
    "ProjectStatus",
    "ProjectView",
    "ProjectMemberView",
    "CreateProjectRequest",
    "UpdateProjectRequest",
    "AddMemberRequest",
    "UpdateMemberRoleRequest",
    "ProjectListQuery",
    "ProjectPage",
    # 后续任务占位
    "ProjectFutureView",
    "CreateProjectFutureRequest",
    "UpdateProjectFutureRequest",
    "ProjectFutureQuery",
    "AddProjectInputRequest",
    "ProjectInputView",
    "BindRepositoryRequest",
    "RepositoryBindingView",
    "CreateChangeRequest",
    "ChangeRequestView",
]
