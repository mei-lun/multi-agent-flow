"""SQLite 连接、PRAGMA、迁移和单进程写协调接口。"""

from typing import AsyncIterator, Protocol


class Database(Protocol):
    async def initialize(self) -> None:
        """创建数据目录、打开 maf.db、应用 PRAGMA 和顺序 migration；不能启动业务任务。"""
        ...
    async def read_connection(self) -> AsyncIterator[object]:
        """提供只读短连接；调用结束自动关闭。"""
        ...
    async def write_connection(self) -> AsyncIterator[object]:
        """经进程内协调器提供短 BEGIN IMMEDIATE 事务；禁止跨网络 await 持有。"""
        ...
    async def close(self) -> None:
        """停止接收新事务并等待现有短事务结束后关闭连接。"""
        ...


class SQLiteWriteCoordinator(Protocol):
    async def acquire(self) -> AsyncIterator[None]:
        """串行化短写事务；只解决本 Server 进程竞争，不是分布式锁。"""
        ...

