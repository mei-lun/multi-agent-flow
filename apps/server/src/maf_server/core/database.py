"""SQLite 连接、PRAGMA、迁移和单进程写协调接口。

根据《多 Agent 协同工具系统设计文档》6.0/6.6 节与《GitHub 分布式协作协议》：

- ``maf.db`` 与 ``checkpoints.db`` 是两个独立 SQLite 数据库，降低锁竞争；
  业务表使用 ``maf.db``，LangGraph checkpoint 使用 ``checkpoints.db``。
- 启动时应用基线 PRAGMA（WAL、foreign_keys、busy_timeout、synchronous、temp_store）；
  WAL 为持久 PRAGMA，其余为 per-connection，每次打开连接重新应用。
- 进程内通过 ``SQLiteWriteCoordinator(asyncio.Lock)`` 串行化 ``BEGIN IMMEDIATE``
  短写事务；普通查询使用独立只读连接，不经协调器。
- 写事务必须短：只做 SQL，不在事务中调用模型、Docker、Git 或网络。
- SQLite 是 Git control 分支的可重建投影（rebuildable projection），不是事实源；
  单写者原则在 Git 层，SQLite 写入仍需正确处理本进程并发。

本模块只提供连接与协调原语，不创建业务表（由迁移负责），也不启动业务任务。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import TracebackType
from typing import Final, Literal

import aiosqlite

from maf_server.config import ServerSettings

# --------------------------------------------------------------------------- #
# PRAGMA 基线（与 infra/sqlite/pragmas.sql 保持一致；来源：设计文档 6.0 节）
# --------------------------------------------------------------------------- #

_PRAGMA_STATEMENTS: Final[tuple[str, ...]] = (
    "PRAGMA journal_mode = WAL;",
    "PRAGMA foreign_keys = ON;",
    "PRAGMA busy_timeout = 5000;",
    "PRAGMA synchronous = NORMAL;",
    "PRAGMA temp_store = MEMORY;",
)

#: 启动后期望的 PRAGMA 值，用于自检与测试。
EXPECTED_PRAGMAS: Final[dict[str, object]] = {
    "journal_mode": "wal",
    "foreign_keys": 1,
    "busy_timeout": 5000,
    "synchronous": 1,  # NORMAL
    "temp_store": 2,  # MEMORY
}

#: 受支持的目标数据库标识。
DbTarget = Literal["business", "checkpointer"]


async def _apply_pragmas_async(conn: aiosqlite.Connection) -> None:
    """对异步连接应用基线 PRAGMA。WAL 为持久 PRAGMA，重复设置幂等。"""
    for stmt in _PRAGMA_STATEMENTS:
        await conn.execute(stmt)


def _apply_pragmas_sync(conn: sqlite3.Connection) -> None:
    """对同步连接应用基线 PRAGMA。"""
    for stmt in _PRAGMA_STATEMENTS:
        conn.execute(stmt)


class SQLiteWriteCoordinator:
    """进程内串行化 ``BEGIN IMMEDIATE`` 短写事务的协调器。

    根据设计文档 6.6 节，``maf-server`` 必须以单个 Uvicorn worker 运行；
    进程内通过 ``asyncio.Lock`` 串行化需要 ``BEGIN IMMEDIATE`` 的关键写入，
    避免同进程多协程同时争抢 SQLite 写锁导致 ``database is locked``。

    本协调器只解决本 Server 进程的协程竞争，不是分布式锁；Git 层的单写者
    原则由 ``maf/control`` 分支保护提供。写事务必须短，禁止跨网络 ``await``
    持有本锁。
    """

    def __init__(self) -> None:
        self._lock: asyncio.Lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        """串行化短写事务；只解决本 Server 进程竞争，不是分布式锁。

        持有期间禁止跨网络 ``await``，避免长时间阻塞其他写事务。
        """
        await self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()

    def locked(self) -> bool:
        """是否有写事务正在持有协调器锁。"""
        return self._lock.locked()


class Database:
    """管理 ``maf.db`` 与 ``checkpoints.db`` 两个独立 SQLite 数据库连接。

    - ``business`` 库保存 Git control 分支的可重建投影（业务表）；
    - ``checkpointer`` 库保存 LangGraph checkpoint 状态；
    - 启动时对两个库应用基线 PRAGMA；
    - 通过 ``SQLiteWriteCoordinator`` 提供串行化短写连接（``BEGIN IMMEDIATE``）；
    - 普通读使用独立短连接，不经协调器，可并发；
    - 同时提供同步与异步接口；同步接口主要供 Alembic 迁移等场景使用。

    使用方式::

        db = Database(settings)
        await db.initialize()
        async with db.write_connection() as conn:
            await conn.execute("INSERT INTO ...")
        async with db.read_connection() as conn:
            async with conn.execute("SELECT ...") as cur:
                rows = await cur.fetchall()
        await db.close()
    """

    def __init__(self, settings: ServerSettings) -> None:
        self._settings = settings
        self._write_coordinator: SQLiteWriteCoordinator = SQLiteWriteCoordinator()
        self._initialized: bool = False
        self._closed: bool = False

    # ------------------------------------------------------------------ #
    # 属性
    # ------------------------------------------------------------------ #

    @property
    def business_db_path(self) -> Path:
        """业务数据库绝对路径。"""
        return self._settings.business_db_path

    @property
    def checkpointer_db_path(self) -> Path:
        """checkpoint 数据库绝对路径。"""
        return self._settings.checkpointer_db_path

    @property
    def write_coordinator(self) -> SQLiteWriteCoordinator:
        """进程内写协调器。"""
        return self._write_coordinator

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def is_closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def initialize(self) -> None:
        """创建数据目录、打开两个数据库并应用基线 PRAGMA。

        不会创建业务表（由迁移负责），也不会启动业务任务。可重复调用；
        已初始化时直接返回。WAL 模式为持久 PRAGMA，首次设置后存于数据库头。
        """
        if self._initialized:
            return
        if self._closed:
            raise RuntimeError("Database 已关闭，不能重新初始化")

        if self.business_db_path == self.checkpointer_db_path:
            raise ValueError(
                "business_db_path 与 checkpointer_db_path 不能相同："
                f"{self.business_db_path}"
            )

        # 创建父目录（两个库可能位于不同子目录）
        self.business_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpointer_db_path.parent.mkdir(parents=True, exist_ok=True)

        # 对两个库应用基线 PRAGMA；WAL 在此首次写入数据库头，其余为 per-connection。
        async with aiosqlite.connect(str(self.business_db_path)) as conn:
            await _apply_pragmas_async(conn)
        async with aiosqlite.connect(str(self.checkpointer_db_path)) as conn:
            await _apply_pragmas_async(conn)

        self._initialized = True

    async def close(self) -> None:
        """停止接收新事务并等待现有短写事务结束后关闭。

        短连接模式下没有长期连接需要关闭；此方法先标记关闭状态（拒绝新事务），
        再等待协调器锁释放（现有写事务完成），最后清理初始化标记。
        """
        if self._closed:
            return
        self._closed = True  # 先拒绝新事务
        # 等待现有短写事务结束
        async with self._write_coordinator.acquire():
            pass
        self._initialized = False

    async def __aenter__(self) -> Database:
        await self.initialize()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # 异步连接
    # ------------------------------------------------------------------ #

    @asynccontextmanager
    async def read_connection(
        self, target: DbTarget = "business"
    ) -> AsyncIterator[aiosqlite.Connection]:
        """提供只读短连接；调用结束自动关闭。不经协调器，可并发。

        ``target`` 选择 ``"business"`` 或 ``"checkpointer"`` 数据库。
        """
        self._ensure_ready()
        path = self._select_path(target)
        # isolation_level=None 让 sqlite3 进入 autocommit 模式，
        # 由调用方按需显式管理事务；读连接通常不需要事务。
        conn = await aiosqlite.connect(str(path), isolation_level=None)
        try:
            await _apply_pragmas_async(conn)
            yield conn
        finally:
            await conn.close()

    @asynccontextmanager
    async def write_connection(
        self, target: DbTarget = "business"
    ) -> AsyncIterator[aiosqlite.Connection]:
        """经进程内协调器提供短 ``BEGIN IMMEDIATE`` 事务。

        禁止跨网络 ``await`` 持有；事务必须短，只做 SQL，不在事务中调用
        模型、Docker、Git 或网络。异常时自动 ``ROLLBACK``，正常退出 ``COMMIT``。
        """
        self._ensure_ready()
        path = self._select_path(target)
        async with self._write_coordinator.acquire():
            conn = await aiosqlite.connect(str(path), isolation_level=None)
            try:
                await _apply_pragmas_async(conn)
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except Exception:
                    await conn.execute("ROLLBACK")
                    raise
                else:
                    await conn.execute("COMMIT")
            finally:
                await conn.close()

    # ------------------------------------------------------------------ #
    # 同步连接（供 Alembic 迁移等场景使用）
    # ------------------------------------------------------------------ #

    @contextmanager
    def sync_read_connection(
        self, target: DbTarget = "business"
    ) -> Iterator[sqlite3.Connection]:
        """同步只读短连接；不经协调器。"""
        self._ensure_ready()
        path = self._select_path(target)
        conn = sqlite3.connect(str(path), isolation_level=None)
        try:
            _apply_pragmas_sync(conn)
            yield conn
        finally:
            conn.close()

    @contextmanager
    def sync_write_connection(
        self, target: DbTarget = "business"
    ) -> Iterator[sqlite3.Connection]:
        """同步短写连接（``BEGIN IMMEDIATE``）。

        同步路径不经过 ``asyncio.Lock`` 协调器；依赖 ``busy_timeout`` 在
        SQLite 层重试。主要用于启动期 Alembic 迁移等非并发场景；运行期
        写入应使用异步 ``write_connection`` 经协调器串行化。
        """
        self._ensure_ready()
        path = self._select_path(target)
        conn = sqlite3.connect(str(path), isolation_level=None)
        try:
            _apply_pragmas_sync(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # PRAGMA 自检
    # ------------------------------------------------------------------ #

    async def get_pragma(self, name: str, target: DbTarget = "business") -> object:
        """获取指定 PRAGMA 的当前值。"""
        async with self.read_connection(target) as conn:
            async with conn.execute(f"PRAGMA {name};") as cur:
                row = await cur.fetchone()
            return row[0] if row is not None else None

    async def verify_pragmas(
        self, target: DbTarget = "business"
    ) -> dict[str, object]:
        """返回所有基线 PRAGMA 的当前值，供启动自检与测试使用。"""
        names = (
            "journal_mode",
            "foreign_keys",
            "busy_timeout",
            "synchronous",
            "temp_store",
        )
        result: dict[str, object] = {}
        for name in names:
            result[name] = await self.get_pragma(name, target)
        return result

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _ensure_ready(self) -> None:
        if self._closed:
            raise RuntimeError("Database 已关闭，不能获取连接")
        if not self._initialized:
            raise RuntimeError("Database 未初始化，请先调用 initialize()")

    def _select_path(self, target: DbTarget) -> Path:
        if target == "business":
            return self.business_db_path
        if target == "checkpointer":
            return self.checkpointer_db_path
        raise ValueError(
            f"未知数据库目标 {target!r}，应为 'business' 或 'checkpointer'"
        )


__all__ = [
    "Database",
    "SQLiteWriteCoordinator",
    "EXPECTED_PRAGMAS",
]
