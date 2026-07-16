"""仓库连接和最终变更门禁的应用接口。"""

from typing import Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class RepositoryApplicationService(Protocol):
    async def verify_binding(
        self, actor: ActorContext, binding_id: str, request: VerifyRepositoryRequest
    ) -> RepositoryHealth:
        """验证 GitHub 或 Local Git 绑定。

        读取 Secret 引用并调用 RepositoryAdapter；检查仓库存在、base branch 可解析以及所需
        能力。只执行无破坏读取/临时探测，清理测试分支。保存脱敏状态和 base commit。
        """
        ...

    async def get_run_change(self, actor: ActorContext, run_id: str) -> RepositoryChangeView:
        """返回 Run 的集成分支、head、PR、checks 和 merge 投影；先检查项目权限。"""
        ...

    async def merge_change(
        self, actor: ActorContext, change_id: str, request: MergeRepositoryChangeRequest
    ) -> MergeResultView:
        """执行最终受控合并。

        顺序：验证操作者权限；确认 Final Inbox Decision 为 APPROVE 且主题版本匹配；确认产品
        验收、代码评审、测试和仓库 checks 全部通过；重新读取远端 head 并与
        expected_head_commit 比较；使用幂等 RepositoryCommand 调 Gateway；保存结果与事件。
        任一条件变化都返回冲突，不绕过分支保护。
        """
        ...

