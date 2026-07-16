"""GitHub API Adapter 接口。具体类实现 RepositoryAdapter。"""

from typing import Protocol


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
