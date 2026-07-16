"""Project 模块的请求、响应和查询契约。"""

from typing import Literal, NotRequired, TypedDict

from maf_contracts.common import Money


class ProjectView(TypedDict):
    id: str
    name: str
    description: str
    owner_user_id: str
    data_classification: Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL"]
    default_workflow_version_id: str | None
    budget: Money
    max_run_seconds: int
    status: Literal["ACTIVE", "ARCHIVED"]
    version: int
    created_at: str


class CreateProjectRequest(TypedDict):
    name: str
    description: str
    owner_user_id: str
    data_classification: Literal["PUBLIC", "INTERNAL", "CONFIDENTIAL"]
    default_workflow_version_id: str | None
    budget: Money
    max_run_seconds: int
    idempotency_key: str


class UpdateProjectRequest(TypedDict, total=False):
    name: str
    description: str
    default_workflow_version_id: str | None
    budget: Money
    max_run_seconds: int
    status: Literal["ACTIVE", "ARCHIVED"]
    expected_version: int
    idempotency_key: str


class ProjectQuery(TypedDict, total=False):
    cursor: str
    limit: int
    owner_user_id: str
    status: Literal["ACTIVE", "ARCHIVED"]
    keyword: str


class ProjectPage(TypedDict):
    items: list[ProjectView]
    next_cursor: str | None
    has_more: bool


class AddProjectInputRequest(TypedDict):
    name: str
    content_type: str
    upload_artifact_version_id: str
    change_summary: str
    idempotency_key: str


class ProjectInputView(TypedDict):
    id: str
    project_id: str
    version: int
    name: str
    content_type: str
    artifact_version_id: str
    change_summary: str
    created_at: str


class BindRepositoryRequest(TypedDict):
    repository_type: Literal["GITHUB", "LOCAL_GIT"]
    display_name: str
    location: str
    base_branch: str
    credential_secret_id: NotRequired[str]
    idempotency_key: str


class RepositoryBindingView(TypedDict):
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
    run_id: str
    title: str
    description: str
    affected_requirement_ids: list[str]
    requested_action: Literal["PAUSE_AND_REPLAN", "APPLY_NEXT_NODE", "CANCEL"]
    idempotency_key: str


class ChangeRequestView(TypedDict):
    id: str
    project_id: str
    run_id: str
    status: Literal["PENDING", "APPROVED", "REJECTED", "APPLIED"]
    title: str
    description: str
    affected_requirement_ids: list[str]
    created_at: str

