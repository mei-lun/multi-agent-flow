"""业务数据库事务边界接口。"""

from types import TracebackType
from typing import Protocol


class UnitOfWork(Protocol):
    async def __aenter__(self) -> "UnitOfWork":
        """取得短生命周期连接；不应立即开始写事务直到首次写操作。"""
        ...
    async def commit(self) -> None:
        """原子提交业务修改、幂等记录与 Outbox；只能调用一次。"""
        ...
    async def rollback(self) -> None:
        """回滚未提交修改；可重复调用。"""
        ...
    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        """有异常或未显式 commit 时回滚，并释放连接。"""
        ...

