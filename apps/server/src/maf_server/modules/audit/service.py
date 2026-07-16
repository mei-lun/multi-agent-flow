"""追加式审计接口。"""

from typing import Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class AuditService(Protocol):
    async def record(self, event: AuditEvent) -> None:
        """追加不可修改事件；metadata 写入前移除 Key、Token、密码、Prompt 正文和宿主路径。"""
        ...
    async def query(self, actor: ActorContext, query: AuditQuery) -> AuditPage:
        """仅审计管理员可查询，按 occurred_at + id 稳定分页。"""
        ...

