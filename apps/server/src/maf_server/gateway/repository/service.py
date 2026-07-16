"""供运行、评审和最终合并使用的统一 Repository Gateway。"""

from typing import Protocol
from maf_contracts.repository import *


class RepositoryAdapter(Protocol):
    async def verify(self, binding: dict) -> dict:
        """无破坏验证仓库和所需权限，返回脱敏健康状态。"""
        ...
    async def resolve_base(self, binding: dict, branch: str) -> CommitRef:
        """把 branch 解析为不可变 commit/tree；分支不存在明确失败。"""
        ...
    async def export_base_bundle(self, binding: dict, commit: str) -> str:
        """导出固定 commit 的只读 bundle/source archive，返回 Artifact Version ID。"""
        ...
    async def materialize_change(self, command: RepositoryCommand) -> BranchRef:
        """在受控工作区把 Patch 应用到固定 base，验证 tree 后创建/更新 run 分支。"""
        ...
    async def open_review(self, command: RepositoryCommand) -> ReviewRef:
        """创建 GitHub PR 或本地等价 Review；幂等键防止重复 PR。"""
        ...
    async def get_review(self, ref: ReviewRef) -> RepositoryReviewState:
        """读取实时 head/checks/approval/mergeable，不使用过期缓存作最终合并判断。"""
        ...
    async def merge(self, command: RepositoryCommand) -> MergeResult:
        """只有 expected head 精确匹配时执行配置的 merge method。"""
        ...


class RepositoryGateway(Protocol):
    async def verify_binding(self, binding_id: str) -> dict:
        """解析绑定与 Secret，选择 GitHub/Local Adapter 并返回健康结果。"""
        ...
    async def prepare_workspace(self, command: RepositoryCommand) -> str:
        """固定 base commit 后导出 Runner 可读 Artifact，不向 Runner 下发长期凭据。"""
        ...
    async def materialize_change(self, command: RepositoryCommand) -> BranchRef:
        """校验 Patch 来源、base 和 tree，在 Server 受控仓库生成 integration head。"""
        ...
    async def open_review(self, command: RepositoryCommand) -> ReviewRef:
        """确保分支已物化后创建 review，并保存外部引用。"""
        ...
    async def refresh_review(self, ref: ReviewRef) -> RepositoryReviewState:
        """刷新 PR/本地 Review 投影，产生状态变化事件。"""
        ...
    async def merge_review(self, command: RepositoryCommand) -> MergeResult:
        """再次校验 expected head 后调用 Adapter；Gateway 不自行判断产品 Gate 是否通过。"""
        ...
