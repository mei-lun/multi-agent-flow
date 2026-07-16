"""站内待办创建、查询与决策接口。"""

from typing import Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class InboxService(Protocol):
    async def create(self, request: CreateInboxItem) -> InboxItemView:
        """供内部业务模块幂等创建待办。

        验证 subject 与 assignee，保存当时的 subject_version 和允许决策；同一业务幂等键
        只能有一个 OPEN item。创建后产生 approval.requested 事件，不发送站外通知。
        """
        ...

    async def list_for_actor(self, actor: ActorContext, query: InboxQuery) -> InboxPage:
        """只返回分配给当前用户或其有管理权限的待办。"""
        ...

    async def decide(
        self, actor: ActorContext, item_id: str, request: DecideInboxRequest
    ) -> InboxDecisionView:
        """对 OPEN 待办提交一次不可变人工决策。

        检查 assignee、allowed_decisions、未过期、expected_subject_version 仍是最新；验证允许
        修改的参数白名单；在同一事务保存 Decision、关闭 Item、写事件。提交后由 Scheduler
        Wakeup 消费，接口本身不直接推进图。重复幂等键返回原 Decision。
        """
        ...

