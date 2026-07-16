"""Workspace、Git 变更、提交、分支、检查和 PR 契约。"""

from typing import Literal, TypedDict


class RepositoryCommand(TypedDict):
    operation: Literal["RESOLVE_BASE", "EXPORT_BASE", "MATERIALIZE_CHANGE", "OPEN_REVIEW", "CHECK_REVIEW", "MERGE"]
    repository_binding_id: str
    run_id: str
    task_id: str | None
    attempt_id: str | None
    base_branch: str
    base_commit: str | None
    work_branch: str | None
    expected_head_commit: str | None
    merge_method: str | None
    patch_artifact_version_id: str | None
    idempotency_key: str


class CommitRef(TypedDict):
    commit: str
    tree: str
    branch: str | None


class BranchRef(TypedDict):
    branch: str
    head_commit: str
    tree: str


class ReviewRef(TypedDict):
    provider: Literal["GITHUB", "LOCAL_GIT"]
    external_id: str
    url: str | None
    head_commit: str
    base_commit: str


class RepositoryReviewState(TypedDict):
    state: str
    head_commit: str
    checks_state: str
    approvals: int
    changes_requested: bool
    mergeable: bool | None


class MergeResult(TypedDict):
    status: Literal["MERGED", "CONFLICTED", "FAILED"]
    merge_commit: str | None
    message: str

