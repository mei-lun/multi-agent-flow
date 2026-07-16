"""站内待办公共 HTTP 接口。"""

from typing import Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class InboxHttpApi(Protocol):
    async def get_inbox(self, actor: ActorContext, query: InboxQuery) -> InboxPage:
        """GET `/api/v1/inbox`；返回当前用户站内待办。"""
        ...
    async def post_decision(self, actor: ActorContext, item_id: str, request: DecideInboxRequest) -> InboxDecisionView:
        """POST `/api/v1/inbox/{id}:decide`；决策成功 200，主题版本变化 409。"""
        ...

