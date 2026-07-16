"""Inbox Item 与人工 Decision 持久化接口。"""

from typing import Protocol
from .schemas import *


class InboxRepository(Protocol):
    async def get(self, item_id: str) -> InboxItemView | None:
        """返回待办当前快照；不存在为 None。权限由 Service 判断。"""
        ...
    async def list_for_user(self, user_id: str, query: InboxQuery) -> InboxPage:
        """只查询分配给 user_id 的项目，并按状态/类型游标分页。"""
        ...
    async def save_item(self, item: InboxItemView) -> InboxItemView:
        """按业务幂等键创建 Item；已有相同 Item 时返回原记录。"""
        ...
    async def save_decision_and_close(self, item: InboxItemView, decision: InboxDecisionView) -> InboxDecisionView:
        """必须原子写 Decision 并把 OPEN Item 关闭，避免重复决定。"""
        ...
