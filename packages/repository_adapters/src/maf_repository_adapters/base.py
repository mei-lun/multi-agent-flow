"""仓库供应商无关接口。"""

from typing import Protocol
from maf_contracts.repository import *


class RepositoryAdapter(Protocol):
    async def verify(self, binding: dict) -> dict:
        """输入脱敏绑定/短期凭据，输出读取、分支、Review 能力健康状态。"""
        ...
    async def resolve_base(self, binding: dict, branch: str) -> CommitRef:
        """把可变分支解析为不可变 commit/tree。"""
        ...
    async def export_base_bundle(self, binding: dict, commit: str) -> str:
        """输出固定源代码 Artifact Version ID，不输出凭据。"""
        ...
    async def materialize_change(self, command: RepositoryCommand) -> BranchRef:
        """验证并应用 Patch，输出 integration branch/head。"""
        ...
    async def open_review(self, command: RepositoryCommand) -> ReviewRef:
        """幂等创建 PR 或本地 Review。"""
        ...
    async def get_review(self, ref: ReviewRef) -> RepositoryReviewState:
        """读取实时 head/checks/approval/mergeable。"""
        ...
    async def merge(self, command: RepositoryCommand) -> MergeResult:
        """仅在 expected head 匹配时执行指定合并方式。"""
        ...

