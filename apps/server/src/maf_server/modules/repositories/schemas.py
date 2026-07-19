"""仓库绑定、验证、运行变更、PR 状态和最终合并契约。

TASK-035 范围新增：
- ``RepositoryBindingStatus``：绑定验证状态字面量。
- ``RepositoryBindingView``：仓库绑定对外视图（不含明文凭据）。
- ``RepositoryInfo``：适配器验证返回的仓库元信息。
- ``BindRepositoryRequest``：绑定仓库请求体。

保留 TASK-083+ 占位 schema（VerifyRepositoryRequest、RepositoryHealth、
RepositoryChangeView、MergeRepositoryChangeRequest、MergeResultView）。
"""

from typing import Literal, TypedDict


# --------------------------------------------------------------------------- #
# TASK-035: 仓库绑定与验证
# --------------------------------------------------------------------------- #

#: 仓库绑定验证状态。``UNVERIFIED`` 为初始，``VERIFIED`` 为验证通过，``FAILED``
#: 为验证失败（URL 不可达、分支不存在、凭据无效或无写权限）。
RepositoryBindingStatus = Literal["UNVERIFIED", "VERIFIED", "FAILED"]

#: 凭据类型字面量。``NONE`` 用于无需凭据的本地 file:// 仓库。
CredentialType = Literal["HTTPS_TOKEN", "SSH_KEY", "NONE"]


class RepositoryInfo(TypedDict):
    """适配器验证返回的仓库元信息（脱敏，无凭据）。"""

    default_branch: str
    branches: list[str]
    can_read: bool
    can_write: bool


class RepositoryBindingView(TypedDict):
    """仓库绑定对外视图。

    ``credential_configured`` 表示是否配置了凭据（不暴露 secret_id 或 key 路径）；
    ``credential_type`` 标识凭据方式；其余字段为绑定元数据。
    """

    id: str
    project_id: str
    repository_url: str
    branch: str
    credential_type: CredentialType
    credential_configured: bool
    verified: bool
    verified_at: str | None
    bound_by: str
    bound_at: str
    version: int


class BindRepositoryRequest(TypedDict):
    """``bind_repository`` 请求体（凭据明文仅短暂存在，不入库）。"""

    repository_url: str
    branch: str
    credential_type: CredentialType
    credential_plaintext: str | None  # HTTPS_TOKEN 模式下的 token 明文
    ssh_key_path: str | None  # SSH_KEY 模式下的 key 路径


# --------------------------------------------------------------------------- #
# TASK-083+ 占位 schema（保留，本任务不修改）
# --------------------------------------------------------------------------- #


class VerifyRepositoryRequest(TypedDict):
    idempotency_key: str


class RepositoryHealth(TypedDict):
    status: Literal["READY", "ERROR"]
    base_branch: str
    base_commit: str | None
    can_read: bool
    can_create_branch: bool
    can_create_pull_request: bool
    message: str
    checked_at: str


class RepositoryChangeView(TypedDict):
    id: str
    run_id: str
    repository_binding_id: str
    base_branch: str
    base_commit: str
    work_branch: str | None
    integration_head_commit: str | None
    pull_request_url: str | None
    pull_request_number: int | None
    review_state: str | None
    checks_state: str | None
    merge_state: Literal["NOT_READY", "READY", "MERGING", "MERGED", "CONFLICTED", "FAILED"]
    version: int


class MergeRepositoryChangeRequest(TypedDict):
    expected_head_commit: str
    expected_version: int
    merge_method: Literal["MERGE", "SQUASH", "REBASE"]
    final_inbox_decision_id: str
    idempotency_key: str


class MergeResultView(TypedDict):
    repository_change_id: str
    status: Literal["MERGED", "CONFLICTED", "FAILED"]
    merge_commit: str | None
    message: str
