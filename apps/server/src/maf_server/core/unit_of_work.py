"""业务数据库事务边界接口与 SQLite 实现。

根据《多 Agent 协同工具系统设计文档》6.6 节与《GitHub 分布式协作协议》：

- ``UnitOfWork`` 是业务事务边界协议（``Protocol``），由应用服务层在写用例中
  ``async with`` 使用；它持有短生命周期连接、提交/回滚业务修改、Outbox 与幂等记录。
- ``SqliteUnitOfWork`` 是基于 ``Database`` 与 ``SQLiteWriteCoordinator`` 的具体实现：
  - ``__aenter__`` 经 ``SQLiteWriteCoordinator.acquire()`` 取得进程内写锁，
    打开短连接并应用基线 PRAGMA，然后 ``BEGIN IMMEDIATE``；
  - ``commit()`` 执行 ``COMMIT``（仅一次有效）；
  - ``rollback()`` 执行 ``ROLLBACK``（可重复调用）；
  - ``__aexit__`` 在有异常或未显式 commit 时自动 ``ROLLBACK``，并释放锁与连接。
- ``update_with_expected_version`` 是通用乐观锁更新辅助函数，执行
  ``UPDATE ... WHERE version = expected``，影响行数 0 抛 ``VersionConflictError``。

事务边界约束（协议 §10、§6.6）：

- SQLite 是 Git control 分支的可重建投影，不是事实源；单写者原则在 Git 层；
- 写事务必须短：只做 SQL，不在事务中调用模型、Docker、Git 或网络；
- Git push/pull 绝不在写事务内执行，必须在 ``UnitOfWork`` 提交后进行；
- 本模块不依赖 FastAPI、SQLAlchemy、LangGraph、Docker 或模型 SDK。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import TracebackType
from typing import Protocol

import aiosqlite

from maf_domain.errors import VersionConflictError
from maf_domain.states import VERSION_COLUMN_DEFAULT, VERSION_INITIAL, ExpectedVersion

# 复用 database 模块的 per-connection PRAGMA 应用函数，避免基线 PRAGMA 列表漂移。
# 该函数是 module-level helper（非类私有方法），在 ``maf_server.core`` 包内共享。
from maf_server.core.database import Database, DbTarget, _apply_pragmas_async


class UnitOfWork(Protocol):
    """业务事务边界协议。

    由应用服务层在写用例中 ``async with unit_of_work:`` 使用，协议方法对应
    《接口设计与实现规范》第 6 节实现步骤模板中的事务边界（步骤 6-10）。
    """

    async def __aenter__(self) -> "UnitOfWork":
        """取得短生命周期连接；不应立即开始写事务直到首次写操作。"""
        ...

    async def commit(self) -> None:
        """原子提交业务修改、幂等记录与 Outbox；只能调用一次。"""
        ...

    async def rollback(self) -> None:
        """回滚未提交修改；可重复调用。"""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """有异常或未显式 commit 时回滚，并释放连接。"""
        ...


# --------------------------------------------------------------------------- #
# 乐观锁通用辅助函数
# --------------------------------------------------------------------------- #

#: 合法 SQL 标识符正则（字母/下划线开头，后跟字母/数字/下划线）。
_IDENT_RE: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(name: str) -> str:
    """校验 SQL 标识符（表名/列名），防止通过列名注入。

    ``aiosqlite`` 的参数绑定只对 ``?`` 占位符生效，表名与列名必须拼接到 SQL
    字符串中，因此对调用方传入的标识符做白名单校验。
    """
    if not _IDENT_RE.match(name):
        raise ValueError(f"非法 SQL 标识符: {name!r}")
    return name


async def update_with_expected_version(
    conn: aiosqlite.Connection,
    table: str,
    assignments: Mapping[str, object],
    where: Mapping[str, object],
    expected_version: ExpectedVersion,
    *,
    version_column: str = VERSION_COLUMN_DEFAULT,
) -> int:
    """通用乐观锁更新辅助函数。

    执行::

        UPDATE <table>
        SET <assignments>, <version_column> = <version_column> + 1
        WHERE <where> AND <version_column> = <expected_version>

    影响行数为 0 时抛 ``VersionConflictError``（由 API 层映射为 HTTP 409）。
    成功返回影响行数（通常为 1）。

    谁调用它：
        Repository 实现在 ``UnitOfWork`` 事务内更新带版本号的聚合根时调用。

    输入来源与可信度：
        - ``conn``：由 ``SqliteUnitOfWork.connection`` 提供，已在 ``BEGIN IMMEDIATE``
          事务中，可信；
        - ``table``/``assignments``/``where`` 的键：由 Repository 内部硬编码或
          Schema 校验后传入，需校验为合法 SQL 标识符；
        - ``assignments``/``where`` 的值与 ``expected_version``：来自调用方，
          通过 ``?`` 占位符参数化绑定，安全。

    业务错误：
        - ``VersionConflictError``：影响行数为 0，表示 ``expected_version`` 不匹配
          或目标行不存在；``retryable=True``，调用方可重新读取后重试。
        - ``ValueError``：``assignments`` 为空或标识符非法，调用方编程错误。

    :param conn: 已在事务中的 aiosqlite 连接（由 UnitOfWork 提供）。
    :param table: 表名（仅允许字母/数字/下划线）。
    :param assignments: SET 子句的列名→值映射（不含版本列，版本列由本函数递增）。
    :param where: WHERE 条件的列名→值映射（不含版本条件，版本条件由本函数追加）。
    :param expected_version: 调用方持有的期望版本号（对应行当前 ``version_no``）。
    :param version_column: 版本列名，默认 ``"version_no"``（与设计文档 6.1 节一致）。
    :raises VersionConflictError: 影响行数为 0（版本不匹配或行不存在）。
    :raises ValueError: ``assignments`` 为空或标识符非法。
    :returns: 影响行数（成功时为 1）。
    """
    if not assignments:
        raise ValueError("assignments 不能为空，至少需要一个 SET 列")
    if expected_version < VERSION_INITIAL:
        raise ValueError(
            f"expected_version 必须 >= {VERSION_INITIAL}， got {expected_version}"
        )

    _validate_identifier(table)
    _validate_identifier(version_column)

    set_parts: list[str] = []
    params: list[object] = []
    for col, val in assignments.items():
        _validate_identifier(col)
        set_parts.append(f"{col} = ?")
        params.append(val)
    # 版本列由本函数递增，调用方不应在 assignments 中包含版本列。
    set_parts.append(f"{version_column} = {version_column} + 1")

    where_parts: list[str] = []
    for col, val in where.items():
        _validate_identifier(col)
        where_parts.append(f"{col} = ?")
        params.append(val)
    where_parts.append(f"{version_column} = ?")
    params.append(expected_version)

    sql = (
        f"UPDATE {table} "
        f"SET {', '.join(set_parts)} "
        f"WHERE {' AND '.join(where_parts)}"
    )

    cursor = await conn.execute(sql, params)
    try:
        rowcount = cursor.rowcount
    finally:
        await cursor.close()

    if rowcount == 0:
        raise VersionConflictError(
            f"乐观锁冲突：{table} 期望版本 {expected_version} 不匹配或行不存在",
            context={
                "table": table,
                "expected_version": expected_version,
                "version_column": version_column,
            },
            retryable=True,
        )
    return rowcount


# --------------------------------------------------------------------------- #
# SqliteUnitOfWork 具体实现
# --------------------------------------------------------------------------- #


class SqliteUnitOfWork:
    """基于 ``Database`` 与 ``SQLiteWriteCoordinator`` 的异步 UnitOfWork 实现。

    事务边界与协议：

    - ``__aenter__``：经 ``SQLiteWriteCoordinator.acquire()`` 取得进程内写锁
      （串行化 ``BEGIN IMMEDIATE``，避免同进程协程争抢 SQLite 写锁），
      打开短连接、应用基线 PRAGMA 并 ``BEGIN IMMEDIATE``。
    - ``commit()``：执行 ``COMMIT``，标记已提交；仅一次有效，重复调用抛错。
    - ``rollback()``：执行 ``ROLLBACK``；可重复调用，已 commit 后调用抛错。
    - ``__aexit__``：有异常或未显式 commit 时自动 ``ROLLBACK``；无论成功失败
      都关闭连接并释放协调器锁。

    事务内禁止（协议 §6.6、§10）：

    - 调用 ``GitClient``、``RepositoryGateway`` 等 Git/网络副作用；
    - 调用 Docker、模型 Adapter 等长 IO；
    - 跨网络 ``await`` 持有协调器锁。

    Git push/pull 必须在 ``UnitOfWork`` 提交后执行；本类不接受也不持有
    ``git_client`` 等外部副作用客户端，从结构上保证写事务内不接触网络。

    乐观锁：

    - 在事务内调用 ``update_with_expected_version`` 执行
      ``UPDATE ... WHERE version = expected``；
    - 影响行数 0 抛 ``VersionConflictError``，由 ``__aexit__`` 自动回滚，
      API 层映射为 HTTP 409。

    使用示例::

        async with SqliteUnitOfWork(database) as uow:
            await update_with_expected_version(
                uow.connection, "tasks",
                assignments={"status": "IN_PROGRESS"},
                where={"id": task_id},
                expected_version=task.version_no,
            )
            await uow.commit()
        # commit 成功后才执行 Git push 等外部副作用
    """

    def __init__(self, database: Database, target: DbTarget = "business") -> None:
        self._database: Database = database
        self._target: DbTarget = target
        self._conn: aiosqlite.Connection | None = None
        self._lock_cm: AbstractAsyncContextManager[None] | None = None
        self._committed: bool = False
        self._rolled_back: bool = False
        self._transaction_started: bool = False

    # ------------------------------------------------------------------ #
    # 公开属性
    # ------------------------------------------------------------------ #

    @property
    def connection(self) -> aiosqlite.Connection:
        """当前 UoW 持有的 aiosqlite 连接（已在 ``BEGIN IMMEDIATE`` 事务中）。

        Repository 通过此属性取得连接执行 SQL；写事务已由 ``__aenter__`` 开始，
        调用方只需在结束时调用 ``commit()`` 或 ``rollback()``。
        """
        if self._conn is None:
            raise RuntimeError("UnitOfWork 未进入 __aenter__，无可用连接")
        return self._conn

    @property
    def is_committed(self) -> bool:
        """是否已成功 commit。"""
        return self._committed

    @property
    def is_rolled_back(self) -> bool:
        """是否已 rollback（含异常或未 commit 的自动回滚）。"""
        return self._rolled_back

    @property
    def is_active(self) -> bool:
        """事务是否仍处于活动状态（已进入且未 commit/rollback）。"""
        return (
            self._conn is not None
            and self._transaction_started
            and not self._committed
            and not self._rolled_back
        )

    # ------------------------------------------------------------------ #
    # Protocol: __aenter__ / __aexit__ / commit / rollback
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> SqliteUnitOfWork:
        """取得协调器锁、打开短连接、应用 PRAGMA 并 ``BEGIN IMMEDIATE``。

        经 ``SQLiteWriteCoordinator.acquire()`` 串行化本进程内需要写锁的协程，
        避免同进程多协程同时 ``BEGIN IMMEDIATE`` 触发 ``database is locked``。
        持锁期间禁止跨网络 ``await``。
        """
        self._ensure_database_ready()
        path = self._select_path()

        # 1. 取得进程内写协调器锁（串行化 BEGIN IMMEDIATE）
        self._lock_cm = self._database.write_coordinator.acquire()
        await self._lock_cm.__aenter__()
        try:
            # 2. 打开短连接（autocommit 模式，由本类显式管理事务）
            self._conn = await aiosqlite.connect(str(path), isolation_level=None)
            # 3. 应用基线 PRAGMA（per-connection，每次打开重新应用）
            await _apply_pragmas_async(self._conn)
            # 4. BEGIN IMMEDIATE 立即取得写锁，串行化本进程写事务
            await self._conn.execute("BEGIN IMMEDIATE")
            self._transaction_started = True
        except BaseException:
            # 连接打开或 PRAGMA 或 BEGIN 失败：释放锁并清理
            if self._conn is not None:
                try:
                    await self._conn.close()
                except Exception:
                    pass
                self._conn = None
            if self._lock_cm is not None:
                await self._lock_cm.__aexit__(None, None, None)
                self._lock_cm = None
            self._transaction_started = False
            raise
        return self

    async def commit(self) -> None:
        """提交事务；仅一次有效，已 rollback 后调用抛错。

        提交后事务结束，``__aexit__`` 不再回滚。调用方应在 ``commit()`` 成功
        返回后才执行 Git push 等可延后的外部动作。
        """
        if self._committed:
            raise RuntimeError("UnitOfWork 已 commit，不能重复提交")
        if self._rolled_back:
            raise RuntimeError("UnitOfWork 已 rollback，不能 commit")
        if self._conn is None or not self._transaction_started:
            raise RuntimeError("UnitOfWork 未进入 __aenter__ 或事务未开始")

        await self._conn.execute("COMMIT")
        self._transaction_started = False
        self._committed = True

    async def rollback(self) -> None:
        """回滚事务；可重复调用，已 commit 后调用抛错。"""
        if self._committed:
            raise RuntimeError("UnitOfWork 已 commit，不能 rollback")
        if self._rolled_back:
            return  # 幂等：已回滚则直接返回
        if self._conn is not None and self._transaction_started:
            await self._conn.execute("ROLLBACK")
            self._transaction_started = False
        self._rolled_back = True

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """有异常或未显式 commit 时自动回滚，并释放连接与协调器锁。

        无论是否发生异常，都关闭连接并释放协调器锁，保证不泄漏资源。
        回滚失败不掩盖原始异常（若原始有异常，回滚错误被吞掉；若无异常且
        回滚失败，则向上抛出回滚错误）。
        """
        rollback_error: BaseException | None = None
        try:
            need_rollback = (
                exc_type is not None or not self._committed
            ) and not self._rolled_back
            if need_rollback:
                if self._conn is not None and self._transaction_started:
                    try:
                        await self._conn.execute("ROLLBACK")
                        self._transaction_started = False
                    except BaseException as err:
                        rollback_error = err
                self._rolled_back = True
        finally:
            # 关闭连接（best-effort）
            if self._conn is not None:
                try:
                    await self._conn.close()
                except Exception:
                    pass
                self._conn = None
            # 释放协调器锁
            if self._lock_cm is not None:
                await self._lock_cm.__aexit__(None, None, None)
                self._lock_cm = None

        # 无原始异常但回滚失败时抛出回滚错误；有原始异常时优先抛原始异常
        if rollback_error is not None and exc_type is None:
            raise rollback_error

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _ensure_database_ready(self) -> None:
        """校验 Database 已初始化且未关闭。"""
        if self._database.is_closed:
            raise RuntimeError("Database 已关闭，不能获取连接")
        if not self._database.is_initialized:
            raise RuntimeError("Database 未初始化，请先调用 initialize()")

    def _select_path(self) -> Path:
        """根据 target 选择数据库文件路径。"""
        if self._target == "business":
            return self._database.business_db_path
        if self._target == "checkpointer":
            return self._database.checkpointer_db_path
        raise ValueError(
            f"未知数据库目标 {self._target!r}，应为 'business' 或 'checkpointer'"
        )


__all__ = [
    "UnitOfWork",
    "SqliteUnitOfWork",
    "update_with_expected_version",
]
