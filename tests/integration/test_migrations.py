"""TASK-007 集成测试：数据库迁移器。

验收标准：
1. 空库可迁移到最新版本。
2. 重复执行不重复建表。
3. 已应用迁移被修改时启动失败并提示校验和冲突。

测试范围：
- ``migrations/runner.py``：``MigrationRunner``、``Migration``、``MigrationError``。
- ``migrations/__init__.py``：包导出。

测试策略：
- 在 ``tmp_path`` 下创建临时迁移目录与 SQLite 数据库，避免污染仓库；
- 不依赖已提交的迁移脚本（本任务不实现业务表），所有迁移由测试临时构造；
- 覆盖发现、排序、应用、幂等、校验和冲突、事务回滚、CLI 入口等路径。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

# 项目根不在 pyproject.toml 的 pythonpath 中，需要显式加入才能导入 migrations 包。
# 这是测试专用的路径设置，不污染生产代码。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from migrations.runner import (  # noqa: E402  -- sys.path 注入后的本地导入
    Migration,
    MigrationError,
    MigrationRunner,
)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def migrations_dir(tmp_path: Path) -> Path:
    """临时迁移目录，测试结束后随 tmp_path 清理。"""
    d = tmp_path / "migrations"
    d.mkdir()
    return d


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """临时 SQLite 数据库路径。"""
    return tmp_path / "maf.db"


def _write_migration(
    migrations_dir: Path,
    version: str,
    description: str,
    sql: str,
) -> Path:
    """写入一个迁移文件并返回路径。"""
    path = migrations_dir / f"{version}_{description}.sql"
    path.write_text(sql, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 验收 1：空库可迁移到最新版本
# --------------------------------------------------------------------------- #


class TestEmptyDbMigratesToLatest:
    """验收标准 1：空库可迁移到最新版本。"""

    def test_empty_db_applies_all_migrations(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """空库执行迁移后，所有迁移应被应用且记录在 schema_migrations。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        _write_migration(
            migrations_dir,
            "0002",
            "add_users",
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL);\n",
        )

        runner = MigrationRunner(migrations_dir, db_path)
        applied = runner.run()

        assert applied == ["0001", "0002"]

        # 业务表存在
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = {row[0] for row in cur.fetchall()}
            cur.close()
        assert {"t1", "users", "schema_migrations"}.issubset(tables)

    def test_run_creates_db_file_if_missing(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """数据库文件不存在时，runner 应自动创建。"""
        assert not db_path.exists()
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )

        runner = MigrationRunner(migrations_dir, db_path)
        runner.run()

        assert db_path.exists()

    def test_run_applies_migrations_in_version_order(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """迁移按版本号升序应用，而非文件系统顺序。"""
        # 故意逆序写入
        _write_migration(
            migrations_dir,
            "0003",
            "third",
            "CREATE TABLE t3 (id INTEGER PRIMARY KEY);\n",
        )
        _write_migration(
            migrations_dir,
            "0001",
            "first",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        _write_migration(
            migrations_dir,
            "0002",
            "second",
            "CREATE TABLE t2 (id INTEGER PRIMARY KEY);\n",
        )

        runner = MigrationRunner(migrations_dir, db_path)
        applied = runner.run()
        assert applied == ["0001", "0002", "0003"]

    def test_run_with_no_migrations_returns_empty(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """无迁移脚本时 run() 返回空列表且创建 schema_migrations 表。"""
        runner = MigrationRunner(migrations_dir, db_path)
        applied = runner.run()
        assert applied == []

        # schema_migrations 表仍应存在
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='schema_migrations'"
            )
            row = cur.fetchone()
            cur.close()
        assert row is not None

    def test_run_partial_then_resume(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """第一次应用部分迁移后，新增迁移再运行应只应用新增部分。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        runner = MigrationRunner(migrations_dir, db_path)
        assert runner.run() == ["0001"]

        # 新增 0002
        _write_migration(
            migrations_dir,
            "0002",
            "add_t2",
            "CREATE TABLE t2 (id INTEGER PRIMARY KEY);\n",
        )
        runner2 = MigrationRunner(migrations_dir, db_path)
        assert runner2.run() == ["0002"]

        # 确认两表都存在
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = {row[0] for row in cur.fetchall()}
            cur.close()
        assert {"t1", "t2"}.issubset(tables)


# --------------------------------------------------------------------------- #
# 验收 2：重复执行不重复建表
# --------------------------------------------------------------------------- #


class TestIdempotentRun:
    """验收标准 2：重复执行不重复建表。"""

    def test_repeat_run_skips_applied(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """重复执行 run() 不重复应用已记录的迁移。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )

        runner = MigrationRunner(migrations_dir, db_path)
        assert runner.run() == ["0001"]
        # 重复执行
        assert runner.run() == []
        assert runner.run() == []

    def test_repeat_run_does_not_duplicate_schema_migrations(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """重复执行不向 schema_migrations 写入重复记录。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        _write_migration(
            migrations_dir,
            "0002",
            "add_t2",
            "CREATE TABLE t2 (id INTEGER PRIMARY KEY);\n",
        )

        runner = MigrationRunner(migrations_dir, db_path)
        runner.run()
        runner.run()
        runner.run()

        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute("SELECT COUNT(*) FROM schema_migrations")
            count = cur.fetchone()[0]
            cur.close()
        assert count == 2

    def test_repeat_run_preserves_applied_at(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """重复执行不更新已记录的 applied_at。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )

        runner = MigrationRunner(migrations_dir, db_path)
        runner.run()

        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "SELECT applied_at FROM schema_migrations WHERE version='0001'"
            )
            first_applied_at = cur.fetchone()[0]
            cur.close()

        runner.run()

        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "SELECT applied_at FROM schema_migrations WHERE version='0001'"
            )
            second_applied_at = cur.fetchone()[0]
            cur.close()

        assert first_applied_at == second_applied_at

    def test_create_table_if_not_exists_safe_on_repeat(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """使用 IF NOT EXISTS 的迁移在重复执行时不会报错（额外保障）。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE IF NOT EXISTS t1 (id INTEGER PRIMARY KEY);\n",
        )
        runner = MigrationRunner(migrations_dir, db_path)
        runner.run()

        # 模拟“遗忘”已应用状态：直接删除 schema_migrations 记录后再运行
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("DELETE FROM schema_migrations;")

        # 由于 IF NOT EXISTS，重复创建不会失败
        applied = runner.run()
        assert applied == ["0001"]


# --------------------------------------------------------------------------- #
# 验收 3：已应用迁移被修改时启动失败并提示校验和冲突
# --------------------------------------------------------------------------- #


class TestChecksumConflict:
    """验收标准 3：已应用迁移被修改时启动失败并提示校验和冲突。"""

    def test_modified_migration_raises_checksum_conflict(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """已应用迁移文件被修改后，run() 应报校验和冲突。"""
        path = _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        runner = MigrationRunner(migrations_dir, db_path)
        runner.run()

        # 修改已应用迁移文件内容
        path.write_text(
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY, name TEXT);\n",
            encoding="utf-8",
        )

        with pytest.raises(MigrationError, match="校验和冲突"):
            runner.run()

    def test_checksum_conflict_message_includes_filename(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """校验和冲突错误信息应包含文件名，便于定位。"""
        path = _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        runner = MigrationRunner(migrations_dir, db_path)
        runner.run()

        path.write_text("-- modified\nCREATE TABLE t1 (id INTEGER);\n", encoding="utf-8")

        with pytest.raises(MigrationError, match="0001_initial.sql"):
            runner.run()

    def test_whitespace_change_triggers_conflict(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """即使是空白字符变化也应触发校验和冲突。"""
        path = _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        runner = MigrationRunner(migrations_dir, db_path)
        runner.run()

        # 添加尾部空格
        path.write_text(
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);   \n",
            encoding="utf-8",
        )

        with pytest.raises(MigrationError, match="校验和冲突"):
            runner.run()

    def test_unmodified_migration_does_not_raise(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """未修改的已应用迁移不应触发校验和冲突。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        _write_migration(
            migrations_dir,
            "0002",
            "add_t2",
            "CREATE TABLE t2 (id INTEGER PRIMARY KEY);\n",
        )
        runner = MigrationRunner(migrations_dir, db_path)
        runner.run()

        # 未修改任何文件，再次运行应正常
        applied = runner.run()
        assert applied == []

    def test_checksum_is_sha256_hex(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """schema_migrations.checksum 应为 64 位小写 hex（SHA-256）。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        runner = MigrationRunner(migrations_dir, db_path)
        runner.run()

        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version='0001'"
            )
            checksum = cur.fetchone()[0]
            cur.close()

        assert len(checksum) == 64
        assert all(c in "0123456789abcdef" for c in checksum)

    def test_only_modified_migration_raises_not_others(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """多个迁移中只有被修改的那个应触发冲突（前序未修改的不应报错）。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        path2 = _write_migration(
            migrations_dir,
            "0002",
            "add_t2",
            "CREATE TABLE t2 (id INTEGER PRIMARY KEY);\n",
        )
        runner = MigrationRunner(migrations_dir, db_path)
        runner.run()

        # 只修改 0002
        path2.write_text(
            "CREATE TABLE t2 (id INTEGER PRIMARY KEY, val TEXT);\n",
            encoding="utf-8",
        )

        with pytest.raises(MigrationError) as exc_info:
            runner.run()
        # 错误信息指向 0002 而非 0001
        assert "0002_add_t2.sql" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# 事务与回滚
# --------------------------------------------------------------------------- #


class TestTransactionRollback:
    """迁移失败时应回滚，不留半写入状态。"""

    def test_failed_migration_rolls_back(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """迁移 SQL 失败时，该迁移的所有变更应回滚，schema_migrations 不记录。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        # 0002 故意写错语法
        _write_migration(
            migrations_dir,
            "0002",
            "bad",
            "CREATE TABLE t2 (id INTEGER PRIMARY KEY;\n",  # 缺少右括号
        )

        runner = MigrationRunner(migrations_dir, db_path)
        with pytest.raises(MigrationError, match="执行失败"):
            runner.run()

        # 0001 应已应用，0002 未记录
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
            versions = [row[0] for row in cur.fetchall()]
            cur.close()
        assert versions == ["0001"]

        # t2 表不应存在（回滚）
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='t2'"
            )
            row = cur.fetchone()
            cur.close()
        assert row is None

    def test_failed_migration_does_not_block_retry(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """失败的迁移修正后可再次运行并成功。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        bad_path = _write_migration(
            migrations_dir,
            "0002",
            "add_t2",
            "CREATE TABLE t2 (id INTEGER PRIMARY KEY;\n",
        )

        runner = MigrationRunner(migrations_dir, db_path)
        with pytest.raises(MigrationError):
            runner.run()

        # 修正 0002（注意：这是新文件，尚未被记录为 applied，所以修改不触发校验和冲突）
        bad_path.write_text(
            "CREATE TABLE t2 (id INTEGER PRIMARY KEY);\n",
            encoding="utf-8",
        )

        applied = runner.run()
        assert applied == ["0002"]

    def test_partial_migration_sql_rolls_back(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """迁移中部分语句失败时，前面已执行的语句也应回滚（事务原子性）。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            (
                "CREATE TABLE t2 (id INTEGER PRIMARY KEY);\n"
                "INSERT INTO t2 (id) VALUES (1);\n"
                "CREATE TABLE t3 (id INTEGER PRIMARY KEY;\n"  # 语法错误
            ),
        )

        runner = MigrationRunner(migrations_dir, db_path)
        with pytest.raises(MigrationError):
            runner.run()

        # t2 不应存在（整个事务回滚）
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='t2'"
            )
            row = cur.fetchone()
            cur.close()
        assert row is None

        # schema_migrations 不应有记录
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute("SELECT COUNT(*) FROM schema_migrations")
            count = cur.fetchone()[0]
            cur.close()
        assert count == 0


# --------------------------------------------------------------------------- #
# 发现与校验
# --------------------------------------------------------------------------- #


class TestDiscovery:
    """迁移发现、排序与文件名校验。"""

    def test_discover_ignores_non_sql_files(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """非 .sql 文件（如 README.md）应被忽略。"""
        (migrations_dir / "README.md").write_text("docs", encoding="utf-8")
        (migrations_dir / "notes.txt").write_text("notes", encoding="utf-8")
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )

        runner = MigrationRunner(migrations_dir, db_path)
        migrations = runner.discover()
        assert len(migrations) == 1
        assert migrations[0].version == "0001"

    def test_discover_rejects_bad_filename(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """不符合 NNNN_description.sql 的文件名应报错。"""
        # 覆盖常见格式错误：序号位数不足、大写、连字符、缺描述、大写描述
        bad_names = [
            "1_initial.sql",
            "0001-Initial.sql",
            "0001_.sql",
            "0001_Init.sql",
        ]
        for name in bad_names:
            (migrations_dir / name).write_text("-- x\n", encoding="utf-8")

        runner = MigrationRunner(migrations_dir, db_path)
        with pytest.raises(MigrationError, match="NNNN_description.sql"):
            runner.discover()

    def test_discover_rejects_non_consecutive_versions(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """版本号跳号应报错。"""
        _write_migration(
            migrations_dir,
            "0001",
            "first",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        _write_migration(
            migrations_dir,
            "0003",
            "third",
            "CREATE TABLE t3 (id INTEGER PRIMARY KEY);\n",
        )

        runner = MigrationRunner(migrations_dir, db_path)
        with pytest.raises(MigrationError, match="不连续"):
            runner.discover()

    def test_discover_rejects_versions_not_starting_from_0001(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """版本号不从 0001 开始应报错。"""
        _write_migration(
            migrations_dir,
            "0002",
            "second",
            "CREATE TABLE t2 (id INTEGER PRIMARY KEY);\n",
        )

        runner = MigrationRunner(migrations_dir, db_path)
        with pytest.raises(MigrationError, match="不连续"):
            runner.discover()

    def test_discover_returns_empty_for_empty_dir(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """空迁移目录返回空列表。"""
        runner = MigrationRunner(migrations_dir, db_path)
        assert runner.discover() == []

    def test_discover_rejects_missing_dir(self, tmp_path: Path, db_path: Path) -> None:
        """迁移目录不存在应报错。"""
        runner = MigrationRunner(tmp_path / "does_not_exist", db_path)
        with pytest.raises(MigrationError, match="目录不存在"):
            runner.discover()

    def test_migration_checksum_stable(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """同一文件多次计算 checksum 结果一致。"""
        path = _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        from migrations.runner import Migration as M

        m = M(version="0001", description="initial", path=path)
        assert m.checksum == m.checksum

    def test_migration_checksum_changes_with_content(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """文件内容变化后 checksum 不同。"""
        path = _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        from migrations.runner import Migration as M

        m1 = M(version="0001", description="initial", path=path)
        # checksum 是惰性属性（每次读取文件），必须在修改前捕获
        checksum_before = m1.checksum
        path.write_text("CREATE TABLE t1 (id INTEGER);\n", encoding="utf-8")
        m2 = M(version="0001", description="initial", path=path)
        assert checksum_before != m2.checksum


# --------------------------------------------------------------------------- #
# schema_migrations 表结构
# --------------------------------------------------------------------------- #


class TestSchemaMigrationsTable:
    """schema_migrations 表结构与记录完整性。"""

    def test_table_has_expected_columns(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """schema_migrations 表应包含 5 个预期列。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        runner = MigrationRunner(migrations_dir, db_path)
        runner.run()

        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute("PRAGMA table_info(schema_migrations)")
            columns = {row[1] for row in cur.fetchall()}
            cur.close()

        expected = {"version", "description", "checksum", "applied_at", "filename"}
        assert expected.issubset(columns)

    def test_applied_versions_returns_correct_map(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """applied_versions() 返回 {version: checksum} 映射。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        _write_migration(
            migrations_dir,
            "0002",
            "add_t2",
            "CREATE TABLE t2 (id INTEGER PRIMARY KEY);\n",
        )
        runner = MigrationRunner(migrations_dir, db_path)
        runner.run()

        recorded = runner.applied_versions()
        assert set(recorded.keys()) == {"0001", "0002"}
        # checksum 为 64 位 hex
        for checksum in recorded.values():
            assert len(checksum) == 64

    def test_applied_versions_empty_for_fresh_db(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """未应用任何迁移时 applied_versions() 返回空 dict。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        runner = MigrationRunner(migrations_dir, db_path)
        assert runner.applied_versions() == {}

    def test_record_contains_correct_filename_and_description(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """schema_migrations 记录的 filename 与 description 字段正确。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        runner = MigrationRunner(migrations_dir, db_path)
        runner.run()

        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "SELECT filename, description FROM schema_migrations WHERE version='0001'"
            )
            row = cur.fetchone()
            cur.close()

        assert row is not None
        assert row[0] == "0001_initial.sql"
        assert row[1] == "initial"

    def test_applied_at_is_rfc3339_utc(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """applied_at 应为 UTC RFC3339 格式（以 Z 结尾）。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        runner = MigrationRunner(migrations_dir, db_path)
        runner.run()

        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "SELECT applied_at FROM schema_migrations WHERE version='0001'"
            )
            applied_at = cur.fetchone()[0]
            cur.close()

        # 格式 YYYY-MM-DDTHH:MM:SSZ
        assert applied_at.endswith("Z")
        assert len(applied_at) == 20
        assert applied_at[10] == "T"


# --------------------------------------------------------------------------- #
# CLI 入口
# --------------------------------------------------------------------------- #


class TestCliEntryPoint:
    """``python -m migrations.runner`` 命令行入口。"""

    def test_cli_applies_migrations(
        self, migrations_dir: Path, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """CLI 应成功应用迁移并返回退出码 0。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )

        from migrations.runner import main

        exit_code = main(
            [
                "--db-path",
                str(db_path),
                "--migrations-dir",
                str(migrations_dir),
            ]
        )
        assert exit_code == 0

        captured = capsys.readouterr()
        assert "已应用 1 个迁移" in captured.out

        # 验证迁移已应用
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version='0001'"
            )
            count = cur.fetchone()[0]
            cur.close()
        assert count == 1

    def test_cli_no_migrations_returns_zero(
        self, migrations_dir: Path, db_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """无待应用迁移时 CLI 返回 0 并输出提示。"""
        from migrations.runner import main

        exit_code = main(
            [
                "--db-path",
                str(db_path),
                "--migrations-dir",
                str(migrations_dir),
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "已是最新版本" in captured.out

    def test_cli_returns_nonzero_on_checksum_conflict(
        self,
        migrations_dir: Path,
        db_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """校验和冲突时 CLI 返回非零退出码并输出错误。"""
        path = _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        from migrations.runner import main

        main(["--db-path", str(db_path), "--migrations-dir", str(migrations_dir)])

        # 修改已应用迁移
        path.write_text("CREATE TABLE t1 (id INTEGER);\n", encoding="utf-8")

        exit_code = main(
            [
                "--db-path",
                str(db_path),
                "--migrations-dir",
                str(migrations_dir),
            ]
        )
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "校验和冲突" in captured.err

    def test_cli_returns_nonzero_on_failed_migration(
        self,
        migrations_dir: Path,
        db_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """迁移 SQL 失败时 CLI 返回非零退出码。"""
        _write_migration(
            migrations_dir,
            "0001",
            "bad",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY;\n",
        )
        from migrations.runner import main

        exit_code = main(
            [
                "--db-path",
                str(db_path),
                "--migrations-dir",
                str(migrations_dir),
            ]
        )
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "执行失败" in captured.err


# --------------------------------------------------------------------------- #
# PRAGMA 基线
# --------------------------------------------------------------------------- #


class TestPragmasApplied:
    """迁移器应在新连接上应用 PRAGMA 基线。"""

    def test_wal_mode_after_migration(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """迁移后数据库应为 WAL 模式。"""
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n",
        )
        runner = MigrationRunner(migrations_dir, db_path)
        runner.run()

        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute("PRAGMA journal_mode;")
            mode = cur.fetchone()[0]
            cur.close()
        assert mode == "wal"

    def test_foreign_keys_enforced_during_migration(
        self, migrations_dir: Path, db_path: Path
    ) -> None:
        """迁移执行期间 foreign_keys 应为 ON，FK 违反应导致迁移失败。

        ``PRAGMA foreign_keys`` 是 per-connection 设置，无法通过 runner 之外的
        新连接验证。这里通过 FK 违反应在迁移中失败的副作用，证明 runner 的连接
        确实启用了外键约束。
        """
        _write_migration(
            migrations_dir,
            "0001",
            "initial",
            (
                "CREATE TABLE parent (id INTEGER PRIMARY KEY);\n"
                "CREATE TABLE child (\n"
                "    id INTEGER PRIMARY KEY,\n"
                "    parent_id INTEGER NOT NULL REFERENCES parent(id)\n"
                ");\n"
                "INSERT INTO child (id, parent_id) VALUES (1, 999);\n"
            ),
        )
        runner = MigrationRunner(migrations_dir, db_path)
        with pytest.raises(MigrationError, match="执行失败"):
            runner.run()

        # FK 违反应导致整个迁移回滚，parent/child 表不应存在
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='parent'"
            )
            assert cur.fetchone() is None
            cur.close()


# --------------------------------------------------------------------------- #
# 环境隔离
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_maf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除 MAF_* 环境变量，避免影响测试。"""
    for key in list(os.environ):
        if key.startswith("MAF_"):
            monkeypatch.delenv(key, raising=False)
