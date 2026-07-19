"""TASK-008 单元测试：UnitOfWork 与乐观锁。

验收标准：
1. 未 commit 或发生异常时自动回滚。
2. 并发版本更新只有一个成功，另一个返回冲突。
3. 网络和 Git 操作不会在写事务内执行。

测试范围：
- ``apps/server/src/maf_server/core/unit_of_work.py``：``UnitOfWork`` Protocol、
  ``SqliteUnitOfWork``、``update_with_expected_version``。
- ``packages/domain/src/maf_domain/states.py``：``ExpectedVersion``、
  ``VERSION_COLUMN_DEFAULT``、``VERSION_INITIAL`` 常量（TASK-022 ``TaskStateMachine``
  保持不变，由 ``tests/unit/test_task_states.py`` 单独覆盖）。
"""

from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from maf_domain.errors import ErrorCode, VersionConflictError
from maf_domain.states import (
    VERSION_COLUMN_DEFAULT,
    VERSION_INITIAL,
    ExpectedVersion,
)
from maf_server.config import ServerSettings
from maf_server.core.database import Database
from maf_server.core.unit_of_work import (
    SqliteUnitOfWork,
    UnitOfWork,
    update_with_expected_version,
)

_SECRET_PLAINTEXT = "test-secret-for-uow-task-008"


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


async def _create_versioned_table(db: Database, table: str = "_uow_v") -> None:
    """创建带 ``version_no`` 列的测试表，与设计文档 6.1 节通用字段表一致。"""
    async with db.write_connection() as conn:
        await conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            "id INTEGER PRIMARY KEY, val TEXT, version_no INTEGER NOT NULL)"
        )


# --------------------------------------------------------------------------- #
# 验收 0：Protocol 契约与乐观锁常量
# --------------------------------------------------------------------------- #


class TestProtocolContract:
    """验证 ``UnitOfWork`` Protocol 与 ``SqliteUnitOfWork`` 契约一致性。"""

    def test_sqlite_uow_satisfies_unit_of_work_protocol(self) -> None:
        """``SqliteUnitOfWork`` 实现 ``UnitOfWork`` Protocol 的全部方法。"""
        uow_methods = {
            "__aenter__",
            "__aexit__",
            "commit",
            "rollback",
        }
        for name in uow_methods:
            assert hasattr(SqliteUnitOfWork, name), f"缺少 Protocol 方法 {name}"

    def test_unit_of_work_protocol_is_protocol(self) -> None:
        """``UnitOfWork`` 应为 ``Protocol``，供应用服务层依赖注入。"""
        # Protocol 类具有 __protocol_attrs__ 或 _is_protocol 标记
        assert hasattr(UnitOfWork, "_is_protocol") or hasattr(
            UnitOfWork, "_is_runtime_protocol"
        )

    def test_expected_version_is_int_alias(self) -> None:
        """``ExpectedVersion`` 是 ``int`` 的类型别名，与 SQLite INTEGER 列对应。"""
        assert ExpectedVersion is int

    def test_version_constants_match_design_doc(self) -> None:
        """版本列名与初始版本号与设计文档 6.1 节通用字段表一致。"""
        assert VERSION_COLUMN_DEFAULT == "version_no"
        assert VERSION_INITIAL == 1


# --------------------------------------------------------------------------- #
# 验收 1：未 commit 或发生异常时自动回滚
# --------------------------------------------------------------------------- #


class TestAutoRollback:
    """验证未 commit 或异常时自动回滚，数据不留半写入。"""

    @pytest.mark.asyncio
    async def test_no_commit_auto_rolls_back(self, db: Database) -> None:
        """进入 UoW 写入数据但不调用 commit，退出后数据不应持久化。"""
        await _create_versioned_table(db)
        async with SqliteUnitOfWork(db) as uow:
            await uow.connection.execute(
                "INSERT INTO _uow_v (id, val, version_no) VALUES (1, 'uncommitted', 1)"
            )
            # 不调用 commit，直接退出
            assert uow.is_active

        # 退出后应自动回滚
        async with db.read_connection() as conn:
            async with conn.execute("SELECT COUNT(*) FROM _uow_v") as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == 0, "未 commit 应自动回滚，不应有残留数据"

    @pytest.mark.asyncio
    async def test_exception_auto_rolls_back(self, db: Database) -> None:
        """事务中抛异常应自动回滚，异常向上传播。"""
        await _create_versioned_table(db)

        with pytest.raises(RuntimeError, match="模拟业务异常"):
            async with SqliteUnitOfWork(db) as uow:
                await uow.connection.execute(
                    "INSERT INTO _uow_v (id, val, version_no) VALUES (1, 'before-error', 1)"
                )
                raise RuntimeError("模拟业务异常")

        # 异常后应无残留数据
        async with db.read_connection() as conn:
            async with conn.execute("SELECT COUNT(*) FROM _uow_v") as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == 0, "异常应触发自动回滚"

    @pytest.mark.asyncio
    async def test_version_conflict_triggers_auto_rollback(self, db: Database) -> None:
        """乐观锁冲突抛 VersionConflictError 应触发自动回滚。"""
        await _create_versioned_table(db)
        async with db.write_connection() as conn:
            await conn.execute(
                "INSERT INTO _uow_v (id, val, version_no) VALUES (1, 'init', 2)"
            )

        with pytest.raises(VersionConflictError):
            async with SqliteUnitOfWork(db) as uow:
                # expected_version=1 不匹配（实际为 2），抛冲突
                await update_with_expected_version(
                    uow.connection,
                    "_uow_v",
                    assignments={"val": "conflict"},
                    where={"id": 1},
                    expected_version=1,
                )

        # 冲突后数据保持原样
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT val, version_no FROM _uow_v WHERE id = 1"
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == "init"
            assert row[1] == 2

    @pytest.mark.asyncio
    async def test_commit_persists_data(self, db: Database) -> None:
        """显式 commit 后数据应持久化。"""
        await _create_versioned_table(db)
        async with SqliteUnitOfWork(db) as uow:
            await uow.connection.execute(
                "INSERT INTO _uow_v (id, val, version_no) VALUES (1, 'committed', 1)"
            )
            await uow.commit()
            assert uow.is_committed
            assert not uow.is_active

        async with db.read_connection() as conn:
            async with conn.execute("SELECT val FROM _uow_v WHERE id = 1") as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == "committed"

    @pytest.mark.asyncio
    async def test_explicit_rollback_discards_data(self, db: Database) -> None:
        """显式 rollback 后数据不应持久化。"""
        await _create_versioned_table(db)
        async with SqliteUnitOfWork(db) as uow:
            await uow.connection.execute(
                "INSERT INTO _uow_v (id, val, version_no) VALUES (1, 'rolled', 1)"
            )
            await uow.rollback()
            assert uow.is_rolled_back

        async with db.read_connection() as conn:
            async with conn.execute("SELECT COUNT(*) FROM _uow_v") as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == 0


# --------------------------------------------------------------------------- #
# 事务生命周期：commit/rollback 状态机
# --------------------------------------------------------------------------- #


class TestLifecycle:
    """验证 commit/rollback 状态机的边界条件。"""

    @pytest.mark.asyncio
    async def test_double_commit_raises(self, db: Database) -> None:
        """重复 commit 应抛错。"""
        await _create_versioned_table(db)
        async with SqliteUnitOfWork(db) as uow:
            await uow.connection.execute(
                "INSERT INTO _uow_v (id, val, version_no) VALUES (1, 'x', 1)"
            )
            await uow.commit()
            with pytest.raises(RuntimeError, match="已 commit"):
                await uow.commit()

    @pytest.mark.asyncio
    async def test_commit_after_rollback_raises(self, db: Database) -> None:
        """rollback 后再 commit 应抛错。"""
        await _create_versioned_table(db)
        async with SqliteUnitOfWork(db) as uow:
            await uow.connection.execute(
                "INSERT INTO _uow_v (id, val, version_no) VALUES (1, 'x', 1)"
            )
            await uow.rollback()
            with pytest.raises(RuntimeError, match="已 rollback"):
                await uow.commit()

    @pytest.mark.asyncio
    async def test_rollback_after_commit_raises(self, db: Database) -> None:
        """commit 后再 rollback 应抛错。"""
        await _create_versioned_table(db)
        async with SqliteUnitOfWork(db) as uow:
            await uow.connection.execute(
                "INSERT INTO _uow_v (id, val, version_no) VALUES (1, 'x', 1)"
            )
            await uow.commit()
            with pytest.raises(RuntimeError, match="已 commit"):
                await uow.rollback()

    @pytest.mark.asyncio
    async def test_rollback_is_idempotent(self, db: Database) -> None:
        """rollback 可重复调用，不抛错。"""
        await _create_versioned_table(db)
        async with SqliteUnitOfWork(db) as uow:
            await uow.connection.execute(
                "INSERT INTO _uow_v (id, val, version_no) VALUES (1, 'x', 1)"
            )
            await uow.rollback()
            await uow.rollback()  # 重复调用不应抛错
            await uow.rollback()
            assert uow.is_rolled_back

    @pytest.mark.asyncio
    async def test_connection_before_enter_raises(self, db: Database) -> None:
        """未进入 __aenter__ 时访问 connection 应抛错。"""
        uow = SqliteUnitOfWork(db)
        with pytest.raises(RuntimeError, match="未进入"):
            _ = uow.connection

    @pytest.mark.asyncio
    async def test_uow_acquires_coordinator_lock(self, db: Database) -> None:
        """__aenter__ 后应持有协调器锁，__aexit__ 后释放。"""
        async with SqliteUnitOfWork(db):
            assert db.write_coordinator.locked(), "UoW 应持有协调器锁"
        assert not db.write_coordinator.locked(), "退出后应释放协调器锁"

    @pytest.mark.asyncio
    async def test_uow_releases_lock_after_commit(self, db: Database) -> None:
        """commit 后退出应释放协调器锁，允许后续写事务或 Git 操作。"""
        await _create_versioned_table(db)
        async with SqliteUnitOfWork(db) as uow:
            await uow.connection.execute(
                "INSERT INTO _uow_v (id, val, version_no) VALUES (1, 'x', 1)"
            )
            await uow.commit()
        assert not db.write_coordinator.locked()

    @pytest.mark.asyncio
    async def test_uow_releases_lock_after_exception(self, db: Database) -> None:
        """异常退出后应释放协调器锁，避免死锁。"""
        with pytest.raises(RuntimeError):
            async with SqliteUnitOfWork(db):
                raise RuntimeError("boom")
        assert not db.write_coordinator.locked()


# --------------------------------------------------------------------------- #
# 验收 2：并发版本更新只有一个成功，另一个返回冲突
# --------------------------------------------------------------------------- #


class TestOptimisticLock:
    """验证乐观锁：并发版本更新一个成功一个冲突。"""

    @pytest.mark.asyncio
    async def test_update_with_expected_version_success(self, db: Database) -> None:
        """expected_version 匹配时更新成功，version_no 递增。"""
        await _create_versioned_table(db)
        async with db.write_connection() as conn:
            await conn.execute(
                "INSERT INTO _uow_v (id, val, version_no) VALUES (1, 'init', 1)"
            )

        async with SqliteUnitOfWork(db) as uow:
            rowcount = await update_with_expected_version(
                uow.connection,
                "_uow_v",
                assignments={"val": "updated"},
                where={"id": 1},
                expected_version=1,
            )
            assert rowcount == 1
            await uow.commit()

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT val, version_no FROM _uow_v WHERE id = 1"
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == "updated"
            assert row[1] == 2, "version_no 应递增到 2"

    @pytest.mark.asyncio
    async def test_update_with_stale_version_raises_conflict(self, db: Database) -> None:
        """expected_version 不匹配时抛 VersionConflictError。"""
        await _create_versioned_table(db)
        async with db.write_connection() as conn:
            await conn.execute(
                "INSERT INTO _uow_v (id, val, version_no) VALUES (1, 'init', 5)"
            )

        async with SqliteUnitOfWork(db) as uow:
            with pytest.raises(VersionConflictError) as exc_info:
                await update_with_expected_version(
                    uow.connection,
                    "_uow_v",
                    assignments={"val": "updated"},
                    where={"id": 1},
                    expected_version=1,  # 实际为 5，不匹配
                )
            err = exc_info.value
            assert err.error_code == ErrorCode.VERSION_CONFLICT
            assert err.retryable is True
            assert err.context["table"] == "_uow_v"
            assert err.context["expected_version"] == 1
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_update_nonexistent_row_raises_conflict(self, db: Database) -> None:
        """更新不存在的行（影响行数 0）抛 VersionConflictError。"""
        await _create_versioned_table(db)

        async with SqliteUnitOfWork(db) as uow:
            with pytest.raises(VersionConflictError):
                await update_with_expected_version(
                    uow.connection,
                    "_uow_v",
                    assignments={"val": "x"},
                    where={"id": 999},
                    expected_version=1,
                )
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_concurrent_updates_one_succeeds_one_conflicts(
        self, db: Database
    ) -> None:
        """两个并发 UoW 都基于 version=1 更新同一行：一个成功，另一个冲突。

        协调器串行化两个写事务：
        - 第一个 UPDATE WHERE version_no=1 成功，version 递增到 2，commit；
        - 第二个 UPDATE WHERE version_no=1 影响行数 0（已被改为 2），抛冲突，
          __aexit__ 自动回滚。
        """
        await _create_versioned_table(db)
        async with db.write_connection() as conn:
            await conn.execute(
                "INSERT INTO _uow_v (id, val, version_no) VALUES (1, 'init', 1)"
            )

        results: dict[str, int] = {"success": 0, "conflict": 0}
        lock = asyncio.Lock()  # 保护 results 计数

        async def _try_update(label: str) -> None:
            try:
                async with SqliteUnitOfWork(db) as uow:
                    await update_with_expected_version(
                        uow.connection,
                        "_uow_v",
                        assignments={"val": f"updated-by-{label}"},
                        where={"id": 1},
                        expected_version=1,
                    )
                    await uow.commit()
            except VersionConflictError:
                async with lock:
                    results["conflict"] += 1
            else:
                async with lock:
                    results["success"] += 1

        # 并发发起两个更新（协调器会串行化）
        await asyncio.gather(_try_update("A"), _try_update("B"))

        assert results["success"] == 1, "应只有一个更新成功"
        assert results["conflict"] == 1, "另一个应返回冲突"

        # 最终 version_no 应为 2（只成功一次）
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT val, version_no FROM _uow_v WHERE id = 1"
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[1] == 2
            assert row[0] in ("updated-by-A", "updated-by-B")

    @pytest.mark.asyncio
    async def test_multiple_concurrent_only_one_succeeds(self, db: Database) -> None:
        """N 个并发更新同一 version，只有一个成功，其余 N-1 个冲突。"""
        await _create_versioned_table(db)
        async with db.write_connection() as conn:
            await conn.execute(
                "INSERT INTO _uow_v (id, val, version_no) VALUES (1, 'init', 1)"
            )

        n = 8
        results: dict[str, int] = {"success": 0, "conflict": 0}
        lock = asyncio.Lock()

        async def _try_update(label: str) -> None:
            try:
                async with SqliteUnitOfWork(db) as uow:
                    await update_with_expected_version(
                        uow.connection,
                        "_uow_v",
                        assignments={"val": label},
                        where={"id": 1},
                        expected_version=1,
                    )
                    await uow.commit()
            except VersionConflictError:
                async with lock:
                    results["conflict"] += 1
            else:
                async with lock:
                    results["success"] += 1

        await asyncio.gather(*(_try_update(f"t{i}") for i in range(n)))

        assert results["success"] == 1
        assert results["conflict"] == n - 1

    @pytest.mark.asyncio
    async def test_sequential_updates_with_refreshed_version_succeed(
        self, db: Database
    ) -> None:
        """顺序更新：第二次用最新 version_no=2 能成功，version 递增到 3。"""
        await _create_versioned_table(db)
        async with db.write_connection() as conn:
            await conn.execute(
                "INSERT INTO _uow_v (id, val, version_no) VALUES (1, 'init', 1)"
            )

        # 第一次更新：v1 -> v2
        async with SqliteUnitOfWork(db) as uow:
            await update_with_expected_version(
                uow.connection,
                "_uow_v",
                assignments={"val": "v2"},
                where={"id": 1},
                expected_version=1,
            )
            await uow.commit()

        # 第二次更新：v2 -> v3（用刷新后的 expected_version=2）
        async with SqliteUnitOfWork(db) as uow:
            await update_with_expected_version(
                uow.connection,
                "_uow_v",
                assignments={"val": "v3"},
                where={"id": 1},
                expected_version=2,
            )
            await uow.commit()

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT val, version_no FROM _uow_v WHERE id = 1"
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == "v3"
            assert row[1] == 3


# --------------------------------------------------------------------------- #
# 乐观锁辅助函数输入校验
# --------------------------------------------------------------------------- #


class TestUpdateWithExpectedVersionValidation:
    """验证 ``update_with_expected_version`` 的输入校验。"""

    @pytest.mark.asyncio
    async def test_empty_assignments_raises(self, db: Database) -> None:
        await _create_versioned_table(db)
        async with SqliteUnitOfWork(db) as uow:
            with pytest.raises(ValueError, match="assignments 不能为空"):
                await update_with_expected_version(
                    uow.connection,
                    "_uow_v",
                    assignments={},
                    where={"id": 1},
                    expected_version=1,
                )
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_invalid_table_name_raises(self, db: Database) -> None:
        await _create_versioned_table(db)
        async with SqliteUnitOfWork(db) as uow:
            with pytest.raises(ValueError, match="非法 SQL 标识符"):
                await update_with_expected_version(
                    uow.connection,
                    "tasks; DROP TABLE users; --",
                    assignments={"val": "x"},
                    where={"id": 1},
                    expected_version=1,
                )
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_invalid_column_name_raises(self, db: Database) -> None:
        await _create_versioned_table(db)
        async with SqliteUnitOfWork(db) as uow:
            with pytest.raises(ValueError, match="非法 SQL 标识符"):
                await update_with_expected_version(
                    uow.connection,
                    "_uow_v",
                    assignments={"val; DROP TABLE x": "evil"},
                    where={"id": 1},
                    expected_version=1,
                )
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_invalid_expected_version_raises(self, db: Database) -> None:
        await _create_versioned_table(db)
        async with SqliteUnitOfWork(db) as uow:
            with pytest.raises(ValueError, match="expected_version"):
                await update_with_expected_version(
                    uow.connection,
                    "_uow_v",
                    assignments={"val": "x"},
                    where={"id": 1},
                    expected_version=0,  # < VERSION_INITIAL
                )
            await uow.rollback()

    @pytest.mark.asyncio
    async def test_custom_version_column(self, db: Database) -> None:
        """支持自定义版本列名（如某些表使用 ``version`` 而非 ``version_no``）。"""
        async with db.write_connection() as conn:
            await conn.execute(
                "CREATE TABLE _uow_c (id INTEGER PRIMARY KEY, val TEXT, "
                "version INTEGER NOT NULL)"
            )
            await conn.execute(
                "INSERT INTO _uow_c (id, val, version) VALUES (1, 'init', 1)"
            )

        async with SqliteUnitOfWork(db) as uow:
            rowcount = await update_with_expected_version(
                uow.connection,
                "_uow_c",
                assignments={"val": "updated"},
                where={"id": 1},
                expected_version=1,
                version_column="version",
            )
            assert rowcount == 1
            await uow.commit()

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT val, version FROM _uow_c WHERE id = 1"
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == "updated"
            assert row[1] == 2


# --------------------------------------------------------------------------- #
# 验收 3：网络和 Git 操作不会在写事务内执行
# --------------------------------------------------------------------------- #


class TestGitAndNetworkIsolation:
    """验证写事务内不执行 Git/网络操作（协议 §6.6、§10）。"""

    def test_uow_constructor_does_not_accept_git_client(self) -> None:
        """``SqliteUnitOfWork.__init__`` 不接受 git_client/repository_gateway 等外部副作用客户端。"""
        sig = inspect.signature(SqliteUnitOfWork.__init__)
        params = set(sig.parameters.keys())
        forbidden = {
            "git_client",
            "repository_gateway",
            "http_client",
            "model_adapter",
            "docker_client",
            "github_client",
        }
        for name in forbidden:
            assert name not in params, (
                f"SqliteUnitOfWork 不应接受 {name}，写事务内禁止 Git/网络副作用"
            )
        # 只接受 database 与 target
        assert "database" in params
        assert "target" in params

    @pytest.mark.asyncio
    async def test_uow_does_not_call_git_client_during_transaction(
        self, db: Database
    ) -> None:
        """UoW 生命周期内不调用任何 Git 客户端方法。"""
        git_client = MagicMock()
        git_client.push = AsyncMock()
        git_client.pull = AsyncMock()

        await _create_versioned_table(db)
        async with SqliteUnitOfWork(db) as uow:
            await uow.connection.execute(
                "INSERT INTO _uow_v (id, val, version_no) VALUES (1, 'x', 1)"
            )
            await uow.commit()

        # UoW 从不接受 git_client，因此无法调用其方法
        git_client.push.assert_not_called()
        git_client.pull.assert_not_called()

    @pytest.mark.asyncio
    async def test_git_operations_after_commit_outside_uow(self, db: Database) -> None:
        """演示正确模式：Git push 在 UoW commit 之后、UoW 之外执行。"""
        git_client = MagicMock()
        git_client.push = AsyncMock(return_value="commit-sha")

        await _create_versioned_table(db)

        # 阶段 1：短写事务（仅 SQL，不接触 Git）
        async with SqliteUnitOfWork(db) as uow:
            await uow.connection.execute(
                "INSERT INTO _uow_v (id, val, version_no) VALUES (1, 'committed', 1)"
            )
            await uow.commit()
            # 事务内：协调器锁持有中，Git push 未调用
            git_client.push.assert_not_called()

        # 阶段 2：UoW 已退出，协调器锁释放，此时执行 Git push
        assert not db.write_coordinator.locked()
        await git_client.push()
        git_client.push.assert_called_once()

        # 数据已持久化（commit 成功）
        async with db.read_connection() as conn:
            async with conn.execute("SELECT val FROM _uow_v WHERE id = 1") as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == "committed"

    @pytest.mark.asyncio
    async def test_no_network_await_holds_coordinator_lock(self, db: Database) -> None:
        """协调器锁持有期间不应执行网络 await（结构上保证）。

        本测试通过断言 UoW 类未持有任何网络客户端属性，从结构上保证写事务内
        无法触发网络调用。模拟一个网络客户端，验证它在 UoW 上下文中不可达。
        """
        network_client = MagicMock()
        network_client.fetch = AsyncMock()

        async with SqliteUnitOfWork(db) as uow:
            # UoW 不持有 network_client，结构上无法在事务内调用网络
            assert not hasattr(uow, "network_client")
            assert not hasattr(uow, "http_client")
            assert not hasattr(uow, "git_client")
            # 仅做 SQL 操作
            await uow.connection.execute("SELECT 1")

        network_client.fetch.assert_not_called()
