"""仓库验证、运行变更、PR 状态和最终合并契约。"""

from typing import Literal, TypedDict


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

