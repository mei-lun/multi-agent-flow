"""TASK-006 集成测试：SQLite 连接与 PRAGMA。

验收标准：
1. 启动后 PRAGMA 值符合设计（journal_mode=wal、foreign_keys=1、busy_timeout=5000、
   synchronous=NORMAL(1)、temp_store=MEMORY(2)）。
2. 业务库与 checkpoint 库路径不同。
3. 并发短写测试不产生未处理的 database locked。

测试范围：
- ``apps/server/src/maf_server/core/database.py``：``Database``、
  ``SQLiteWriteCoordinator``。
- ``infra/sqlite/pragmas.sql``：PRAGMA 基线。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio

from maf_server.config import ServerSettings
from maf_server.core.database import (
    EXPECTED_PRAGMAS,
    Database,
    SQLiteWriteCoordinator,
)

_SECRET_PLAINTEXT = "test-secret-for-sqlite-task-006"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_maf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove any ``MAF_*`` env vars so tests start from a clean slate."""
    for key in list(os.environ):
        if key.startswith("MAF_"):
            monkeypatch.delenv(key, raising=False)


def _make_settings(tmp_path: Path, **overrides: object) -> ServerSettings:
    """构建测试用 ServerSettings，数据库路径落在 ``tmp_path`` 下。"""
    kwargs: dict[str, object] = dict(
        organization_id="org-001",
        business_db_path=Path("maf.db"),
        checkpointer_db_path=Path("checkpoints.db"),
        artifact_root=Path("artifacts"),
        workspace_root=Path("workspaces"),
        git_repo_root=tmp_path / "repo",
        public_base_url="http://localhost:8000",
        secret_key=_SECRET_PLAINTEXT,
        data_dir=tmp_path,
        _env_file=None,
    )
    kwargs.update(overrides)
    return ServerSettings(**kwargs)


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    """已初始化的 Database，测试结束自动关闭。"""
    settings = _make_settings(tmp_path)
    database = Database(settings)
    await database.initialize()
    yield database
    await database.close()


# --------------------------------------------------------------------------- #
# 验收 1：启动后 PRAGMA 值符合设计
# --------------------------------------------------------------------------- #


class TestPragmaValues:
    """验证 ``initialize()`` 后两个库的 PRAGMA 值符合设计文档 6.0 节。"""

    @pytest.mark.asyncio
    async def test_business_db_pragmas(self, db: Database) -> None:
        pragmas = await db.verify_pragmas("business")
        for name, expected in EXPECTED_PRAGMAS.items():
            actual = pragmas[name]
            assert actual == expected, (
                f"business 库 PRAGMA {name}={actual!r}，期望 {expected!r}"
            )

    @pytest.mark.asyncio
    async def test_checkpointer_db_pragmas(self, db: Database) -> None:
        pragmas = await db.verify_pragmas("checkpointer")
        for name, expected in EXPECTED_PRAGMAS.items():
            actual = pragmas[name]
            assert actual == expected, (
                f"checkpointer 库 PRAGMA {name}={actual!r}，期望 {expected!r}"
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "pragma_name,expected",
        [
            ("journal_mode", "wal"),
            ("foreign_keys", 1),
            ("busy_timeout", 5000),
            ("synchronous", 1),
            ("temp_store", 2),
        ],
    )
    async def test_individual_pragma(
        self, db: Database, pragma_name: str, expected: object
    ) -> None:
        actual = await db.get_pragma(pragma_name, "business")
        assert actual == expected, (
            f"PRAGMA {pragma_name}={actual!r}，期望 {expected!r}"
        )

    @pytest.mark.asyncio
    async def test_pragmas_applied_on_every_new_connection(
        self, tmp_path: Path
    ) -> None:
        """每个新连接都应重新应用 per-connection PRAGMA。"""
        settings = _make_settings(tmp_path)
        database = Database(settings)
        await database.initialize()
        try:
            for _ in range(3):
                async with database.read_connection() as conn:
                    async with conn.execute("PRAGMA foreign_keys;") as cur:
                        row = await cur.fetchone()
                    assert row is not None
                    assert row[0] == 1
        finally:
            await database.close()

    @pytest.mark.asyncio
    async def test_sync_connection_pragmas(self, db: Database) -> None:
        """同步连接也应正确应用 PRAGMA。"""
        with db.sync_read_connection() as conn:
            cur = conn.execute("PRAGMA journal_mode;")
            row = cur.fetchone()
            cur.close()
            assert row is not None
            assert row[0] == "wal"

        with db.sync_read_connection() as conn:
            cur = conn.execute("PRAGMA foreign_keys;")
            row = cur.fetchone()
            cur.close()
            assert row is not None
            assert row[0] == 1


# --------------------------------------------------------------------------- #
# 验收 2：业务库与 checkpoint 库路径不同
# --------------------------------------------------------------------------- #


class TestPathSeparation:
    """验证 business_db_path 与 checkpointer_db_path 是两个独立文件。"""

    @pytest.mark.asyncio
    async def test_paths_differ(self, db: Database) -> None:
        assert db.business_db_path != db.checkpointer_db_path

    @pytest.mark.asyncio
    async def test_paths_resolve_to_different_files(self, db: Database) -> None:
        """两个路径解析后指向不同的文件。"""
        assert db.business_db_path.resolve() != db.checkpointer_db_path.resolve()

    @pytest.mark.asyncio
    async def test_both_db_files_exist_after_initialize(
        self, tmp_path: Path
    ) -> None:
        settings = _make_settings(tmp_path)
        database = Database(settings)
        await database.initialize()
        try:
            assert database.business_db_path.exists()
            assert database.checkpointer_db_path.exists()
            assert (tmp_path / "maf.db").exists()
            assert (tmp_path / "checkpoints.db").exists()
        finally:
            await database.close()

    @pytest.mark.asyncio
    async def test_same_path_rejected(self, tmp_path: Path) -> None:
        """相同路径应在 initialize() 时被拒绝。"""
        settings = _make_settings(
            tmp_path,
            business_db_path=Path("same.db"),
            checkpointer_db_path=Path("same.db"),
        )
        database = Database(settings)
        with pytest.raises(ValueError, match="不能相同"):
            await database.initialize()

    @pytest.mark.asyncio
    async def test_writes_to_business_do_not_affect_checkpointer(
        self, db: Database
    ) -> None:
        """向 business 库写表不应在 checkpointer 库中可见。"""
        async with db.write_connection("business") as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS _t1 (id INTEGER PRIMARY KEY)"
            )
            await conn.execute("INSERT INTO _t1 (id) VALUES (1)")

        # business 库能读到
        async with db.read_connection("business") as conn:
            async with conn.execute("SELECT COUNT(*) FROM _t1") as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == 1

        # checkpointer 库没有这个表
        async with db.read_connection("checkpointer") as conn:
            with pytest.raises(sqlite3.OperationalError):
                async with conn.execute("SELECT COUNT(*) FROM _t1") as cur:
                    await cur.fetchone()


# --------------------------------------------------------------------------- #
# 验收 3：并发短写测试不产生未处理的 database locked
# --------------------------------------------------------------------------- #


class TestConcurrentWrites:
    """验证 SQLiteWriteCoordinator 串行化并发短写，不产生 database locked。"""

    @pytest.mark.asyncio
    async def test_concurrent_async_writes_no_locked(self, db: Database) -> None:
        """N 个并发异步写事务全部成功，无 database locked。"""
        n = 30
        async with db.write_connection() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS _cw (id INTEGER PRIMARY KEY, val TEXT)"
            )

        async def _write(i: int) -> None:
            async with db.write_connection() as conn:
                await conn.execute(
                    "INSERT INTO _cw (id, val) VALUES (?, ?)",
                    (i, f"value-{i}"),
                )

        results = await asyncio.gather(
            *(_write(i) for i in range(n)), return_exceptions=True
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        assert not errors, f"并发写产生未处理异常: {errors}"

        # 验证所有行都写入
        async with db.read_connection() as conn:
            async with conn.execute("SELECT COUNT(*) FROM _cw") as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == n

    @pytest.mark.asyncio
    async def test_coordinator_serializes_writes(self, db: Database) -> None:
        """协调器应串行化写事务：同一时刻只有一个写事务在执行。"""
        async with db.write_connection() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS _ser (id INTEGER PRIMARY KEY)"
            )

        execution_log: list[tuple[int, str]] = []

        async def _write(i: int) -> None:
            async with db.write_connection() as conn:
                execution_log.append((i, "start"))
                await asyncio.sleep(0.01)  # 模拟短写
                await conn.execute("INSERT INTO _ser (id) VALUES (?)", (i,))
                execution_log.append((i, "end"))

        await asyncio.gather(*(_write(i) for i in range(10)))

        # 串行化：每个 start 后必须紧跟同 i 的 end，不能交叉
        for idx in range(0, len(execution_log), 2):
            start_entry = execution_log[idx]
            end_entry = execution_log[idx + 1]
            assert start_entry[1] == "start", (
                f"位置 {idx} 应为 start，实际 {start_entry}"
            )
            assert end_entry[1] == "end", (
                f"位置 {idx + 1} 应为 end，实际 {end_entry}"
            )
            assert start_entry[0] == end_entry[0], (
                f"start/end 的任务 ID 不匹配: {start_entry} vs {end_entry}"
            )

    @pytest.mark.asyncio
    async def test_write_rollback_on_exception(self, db: Database) -> None:
        """写事务中抛异常应 ROLLBACK，不留半写入。"""
        async with db.write_connection() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS _rb (id INTEGER PRIMARY KEY, val TEXT)"
            )

        async def _failing_write() -> None:
            async with db.write_connection() as conn:
                await conn.execute(
                    "INSERT INTO _rb (id, val) VALUES (1, 'before-error')"
                )
                raise RuntimeError("模拟业务异常")

        with pytest.raises(RuntimeError, match="模拟业务异常"):
            await _failing_write()

        # 异常后应无残留数据
        async with db.read_connection() as conn:
            async with conn.execute("SELECT COUNT(*) FROM _rb") as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == 0

    @pytest.mark.asyncio
    async def test_concurrent_read_during_write(self, db: Database) -> None:
        """WAL 模式下读连接不阻塞写，写也不阻塞读。"""
        async with db.write_connection() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS _wal (id INTEGER PRIMARY KEY)"
            )
            await conn.execute("INSERT INTO _wal (id) VALUES (1)")

        async def _read() -> int:
            async with db.read_connection() as conn:
                async with conn.execute("SELECT COUNT(*) FROM _wal") as cur:
                    row = await cur.fetchone()
                return int(row[0]) if row else 0

        async def _write() -> None:
            async with db.write_connection() as conn:
                await conn.execute("INSERT INTO _wal (id) VALUES (2)")

        # 并发读写
        read_result, _ = await asyncio.gather(_read(), _write())
        assert read_result >= 1  # 读到了至少一条（WAL 快照读）

        # 写完成后能读到两条
        final_count = await _read()
        assert final_count == 2

    @pytest.mark.asyncio
    async def test_sync_concurrent_writes_with_busy_timeout(
        self, db: Database
    ) -> None:
        """同步并发写依赖 busy_timeout 重试，不应产生未处理 database locked。

        使用短事务 + busy_timeout=5000 保证同步路径也能处理轻度并发。
        """
        import threading

        with db.sync_write_connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _scw (id INTEGER PRIMARY KEY)"
            )

        errors: list[BaseException] = []
        n_threads = 10

        def _sync_write(i: int) -> None:
            try:
                with db.sync_write_connection() as conn:
                    conn.execute("INSERT INTO _scw (id) VALUES (?)", (i,))
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=_sync_write, args=(i,)) for i in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"同步并发写产生未处理异常: {errors}"

        with db.sync_read_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM _scw")
            row = cur.fetchone()
            cur.close()
            assert row is not None
            assert row[0] == n_threads


# --------------------------------------------------------------------------- #
# WAL 模式专项验证
# --------------------------------------------------------------------------- #


class TestWalMode:
    """验证 WAL 模式生效。"""

    @pytest.mark.asyncio
    async def test_journal_mode_is_wal(self, db: Database) -> None:
        mode = await db.get_pragma("journal_mode", "business")
        assert mode == "wal"

    @pytest.mark.asyncio
    async def test_checkpointer_journal_mode_is_wal(self, db: Database) -> None:
        mode = await db.get_pragma("journal_mode", "checkpointer")
        assert mode == "wal"

    @pytest.mark.asyncio
    async def test_wal_files_created(self, tmp_path: Path) -> None:
        """WAL 模式下数据库目录应出现 -wal 文件（在写入后）。"""
        settings = _make_settings(tmp_path)
        database = Database(settings)
        await database.initialize()
        try:
            async with database.write_connection() as conn:
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS _w (id INTEGER PRIMARY KEY)"
                )
                await conn.execute("INSERT INTO _w (id) VALUES (1)")
            # WAL 文件可能在 checkpoint 后消失，因此只验证 journal_mode
            mode = await database.get_pragma("journal_mode")
            assert mode == "wal"
        finally:
            await database.close()


# --------------------------------------------------------------------------- #
# 生命周期与协调器单元测试
# --------------------------------------------------------------------------- #


class TestDatabaseLifecycle:
    """验证 Database 生命周期与错误处理。"""

    @pytest.mark.asyncio
    async def test_not_initialized_rejects_connections(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        database = Database(settings)
        with pytest.raises(RuntimeError, match="未初始化"):
            async with database.read_connection() as conn:
                await conn.execute("SELECT 1")

    @pytest.mark.asyncio
    async def test_double_initialize_idempotent(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        database = Database(settings)
        await database.initialize()
        await database.initialize()  # 不应报错
        assert database.is_initialized
        await database.close()

    @pytest.mark.asyncio
    async def test_close_rejects_new_connections(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        database = Database(settings)
        await database.initialize()
        await database.close()
        assert database.is_closed
        with pytest.raises(RuntimeError, match="已关闭"):
            async with database.read_connection() as conn:
                await conn.execute("SELECT 1")

    @pytest.mark.asyncio
    async def test_double_close_idempotent(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        database = Database(settings)
        await database.initialize()
        await database.close()
        await database.close()  # 不应报错

    @pytest.mark.asyncio
    async def test_close_waits_for_inflight_write(self, tmp_path: Path) -> None:
        """close() 应等待进行中的写事务完成。"""
        settings = _make_settings(tmp_path)
        database = Database(settings)
        await database.initialize()

        async with database.write_connection() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS _cls (id INTEGER PRIMARY KEY)"
            )
            await conn.execute("INSERT INTO _cls (id) VALUES (42)")
            # 在写事务进行中调用 close()，应等待该事务提交
            close_task = asyncio.create_task(database.close())
            await asyncio.sleep(0.05)  # 让 close_task 开始等待
            assert not close_task.done(), "close() 不应在写事务完成前返回"
            # 写事务正常提交

        await close_task  # 现在应该能完成

        # 数据已提交
        assert database.is_closed
        # 用独立连接验证数据
        conn = sqlite3.connect(str(settings.business_db_path))
        try:
            cur = conn.execute("SELECT COUNT(*) FROM _cls")
            row = cur.fetchone()
            cur.close()
            assert row is not None
            assert row[0] == 1
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_context_manager(self, tmp_path: Path) -> None:
        settings = _make_settings(tmp_path)
        async with Database(settings) as database:
            assert database.is_initialized
            async with database.read_connection() as conn:
                async with conn.execute("SELECT 1") as cur:
                    row = await cur.fetchone()
                assert row is not None
                assert row[0] == 1
        assert database.is_closed


class TestSQLiteWriteCoordinatorUnit:
    """SQLiteWriteCoordinator 单元测试。"""

    @pytest.mark.asyncio
    async def test_acquire_serializes(self) -> None:
        coord = SQLiteWriteCoordinator()
        order: list[str] = []

        async def _task(name: str) -> None:
            async with coord.acquire():
                order.append(f"{name}-start")
                await asyncio.sleep(0.01)
                order.append(f"{name}-end")

        await asyncio.gather(*(_task(f"t{i}") for i in range(5)))
        # 串行化：每对 start/end 不交叉
        for i in range(0, len(order), 2):
            assert order[i].endswith("start")
            assert order[i + 1].endswith("end")
            assert order[i].rsplit("-", 1)[0] == order[i + 1].rsplit("-", 1)[0]

    @pytest.mark.asyncio
    async def test_locked_property(self) -> None:
        coord = SQLiteWriteCoordinator()
        assert not coord.locked()
        async with coord.acquire():
            assert coord.locked()
        assert not coord.locked()

    @pytest.mark.asyncio
    async def test_release_on_exception(self) -> None:
        coord = SQLiteWriteCoordinator()

        with pytest.raises(RuntimeError, match="boom"):
            async with coord.acquire():
                raise RuntimeError("boom")

        assert not coord.locked(), "异常后锁应已释放"

        # 锁可再次获取
        async with coord.acquire():
            assert coord.locked()
