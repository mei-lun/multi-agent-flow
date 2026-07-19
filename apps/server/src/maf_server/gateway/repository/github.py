"""GitHub API Adapter 接口。具体类实现 RepositoryAdapter。"""

from __future__ import annotations

from typing import Protocol


class GitHubReviewAdapter:
    """Thin, idempotent adapter around an injected GitHub API client.

    The client is responsible for HTTP authentication; this adapter never accepts a token in
    a command or stores it in a Review record.  A stable run/task marker is used before create
    so retries cannot create duplicate pull requests.
    """

    def __init__(self, client: GitHubApiClient) -> None:
        self.client = client
        self._by_marker: dict[str, dict] = {}

    async def open_review(self, *, owner: str, repo: str, head: str, base: str,
                          title: str, body: str, marker: str) -> dict:
        if not marker:
            raise ValueError("review marker is required")
        existing = self._by_marker.get(marker)
        if existing is not None:
            return dict(existing)
        # API implementations may expose marker lookup; otherwise the local map still makes
        # retries in one process idempotent and the marker is persisted in the PR body.
        lookup = getattr(self.client, "find_pull_request_by_marker", None)
        if lookup is not None:
            found = await lookup(owner, repo, marker)
            if found:
                self._by_marker[marker] = dict(found)
                return dict(found)
        result = await self.client.create_pull_request(owner, repo, head, base, title, f"{body}\n\nMAF-MARKER: {marker}")
        self._by_marker[marker] = dict(result)
        return dict(result)

    async def refresh_review(self, *, owner: str, repo: str, number: int) -> dict:
        result = await self.client.get_pull_request(owner, repo, number)
        return {key: value for key, value in dict(result).items() if key not in {"token", "authorization", "secret"}}

    async def merge_review(self, *, owner: str, repo: str, number: int, expected_head: str, method: str = "SQUASH") -> dict:
        state = await self.refresh_review(owner=owner, repo=repo, number=number)
        if state.get("head") not in {None, expected_head} and state.get("head_commit") not in {None, expected_head}:
            return {"status": "CONFLICTED", "message": "pull request head changed", "merge_commit": None}
        result = await self.client.merge_pull_request(owner, repo, number, expected_head, method)
        return dict(result)


__all__ = ["GitHubApiClient", "GitHubReviewAdapter"]


class GitHubApiClient(Protocol):
    async def create_pull_request(self, owner: str, repo: str, head: str, base: str, title: str, body: str) -> dict:
        """创建 PR；实现前先按 run marker 查询是否已经创建。"""
        ...
    async def get_pull_request(self, owner: str, repo: str, number: int) -> dict:
        """返回当前 head SHA、状态和 mergeable；响应必须脱敏。"""
        ...
    async def merge_pull_request(self, owner: str, repo: str, number: int, expected_head: str, method: str) -> dict:
        """使用 expected_head 防止审批后变更；遵守 GitHub 分支保护。"""
        ...
