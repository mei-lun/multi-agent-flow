"""审计事件只追加持久化接口。"""

from typing import Protocol
from .schemas import *


class AuditRepository(Protocol):
    async def append(self, event: AuditEvent) -> None:
        """以唯一 event.id 追加一行；重复同内容幂等，不允许 UPDATE/DELETE。"""
        ...
    async def query(self, query: AuditQuery) -> AuditPage:
        """应用时间/主体/动作/资源过滤并按 occurred_at,id 游标分页。"""
        ...
