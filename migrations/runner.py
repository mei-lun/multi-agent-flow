"""顺序 SQL 迁移执行器。

根据《项目框架与目录职责说明》9.2 节与《多 Agent 协同工具系统设计文档》：

- ``migrations/`` 保存 ``maf.db`` 的不可变顺序迁移，命名为 ``NNNN_description.sql``；
- 已发布的迁移不得修改（通过校验和检测）；
- ``checkpoints.db`` 由 LangGraph SQLite Adapter 自行管理，不在此处迁移；
- SQLite 是 Git control 分支的可重建投影，迁移器用于建立 schema。

``MigrationRunner`` 职责：

- 扫描 ``migrations_dir`` 下的 ``NNNN_description.sql`` 文件并按版本号排序；
- 启动时确保 ``schema_migrations`` 表存在（``CREATE TABLE IF NOT EXISTS``）；
- 对每个待应用迁移在单事务内执行 SQL 并写入 ``schema_migrations``；
- 已应用迁移跳过执行，但校验和必须与记录一致，否则报 ``MigrationError``；
- 重复执行幂等，不重复建表。

本模块只依赖 Python 标准库 ``sqlite3``，不依赖 ``Database`` 或 ``ServerSettings``，
以便在部署脚本和测试中独立使用。PRAGMA 基线与 ``infra/sqlite/pragmas.sql`` 一致。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["Migration", "MigrationError", "MigrationRunner"]


# 迁移文件名格式：NNNN_description.sql，NNNN 为 4 位零填充序号，description 为 [a-z0-9_]+
_MIGRATION_FILENAME_PATTERN = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

# schema_migrations 表 DDL（与 migrations/README.md 文档保持一致）
# 该表由 MigrationRunner 在应用任何迁移前自动创建，本身不作为编号迁移管理。
_SCHEMA_MIGRATIONS_DDL = """\
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT    PRIMARY KEY,
    description TEXT    NOT NULL,
    checksum    TEXT    NOT NULL,
    applied_at  TEXT    NOT NULL,
    filename    TEXT    NOT NULL
);
"""

# PRAGMA 基线（与 infra/sqlite/pragmas.sql、apps/server/.../core/database.py 保持一致）
_PRAGMA_STATEMENTS: tuple[str, ...] = (
    "PRAGMA journal_mode = WAL;",
    "PRAGMA foreign_keys = ON;",
    "PRAGMA busy_timeout = 5000;",
    "PRAGMA synchronous = NORMAL;",
    "PRAGMA temp_store = MEMORY;",
)


class MigrationError(Exception):
    """迁移执行、校验或版本连续性失败。

    携带稳定字符串信息，供 ``scripts/init-db.ps1`` 和启动流程向用户提示。
    """


@dataclass(frozen=True)
class Migration:
    """单个迁移脚本的元数据与内容。

    - ``version``：4 位序号字符串，如 ``"0001"``；
    - ``description``：文件名中下划线后的描述，如 ``"initial"``；
    - ``path``：迁移文件绝对路径。

    ``checksum`` 与 ``sql`` 按需读取文件，不在构造时缓存，避免重复构造开销。
    """

    version: str
    description: str
    path: Path

    @property
    def filename(self) -> str:
        """文件名，如 ``0001_initial.sql``。"""
        return self.path.name

    @property
    def checksum(self) -> str:
        """SHA-256 校验和（hex），基于文件原始字节计算，避免行尾差异。

        任何字节变化（含 BOM、行尾、空格）都会改变校验和，从而被检测。
        """
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    @property
    def sql(self) -> str:
        """迁移 SQL 文本（UTF-8）。"""
        return self.path.read_text(encoding="utf-8")


class MigrationRunner:
    """顺序 SQL 迁移执行器。

    使用方式::

        runner = MigrationRunner(
            migrations_dir=Path("migrations"),
            db_path=Path("data/maf.db"),
        )
        applied = runner.run()

    特性：

    - 幂等：重复调用 ``run()`` 跳过已应用迁移；
    - 校验和：已应用迁移的 checksum 必须与记录一致，否则 ``MigrationError``；
    - 事务性：每个迁移在独立 ``BEGIN IMMEDIATE`` 事务中执行，失败回滚且不记录；
    - 顺序性：按 ``version`` 升序应用，版本号必须从 ``0001`` 开始且连续，否则报错。
    """

    def __init__(self, migrations_dir: Path, db_path: Path) -> None:
        self._migrations_dir = migrations_dir
        self._db_path = db_path

    @property
    def migrations_dir(self) -> Path:
        """迁移脚本目录。"""
        return self._migrations_dir

    @property
    def db_path(self) -> Path:
        """目标 SQLite 数据库路径。"""
        return self._db_path

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #

    def discover(self) -> list[Migration]:
        """扫描 ``migrations_dir``，返回按版本升序排列的迁移列表。

        - 非 ``.sql`` 文件（如 README.md）被忽略；
        - 文件名必须匹配 ``NNNN_description.sql``，否则 ``MigrationError``；
        - 版本号必须从 ``0001`` 开始且连续，否则 ``MigrationError``（避免遗漏）。
        """
        if not self._migrations_dir.is_dir():
            raise MigrationError(
                f"migrations 目录不存在: {self._migrations_dir}"
            )

        migrations: list[Migration] = []
        for entry in sorted(self._migrations_dir.iterdir()):
            if not entry.is_file():
                continue
            if entry.suffix != ".sql":
                continue
            match = _MIGRATION_FILENAME_PATTERN.match(entry.name)
            if not match:
                raise MigrationError(
                    f"迁移文件名不符合 NNNN_description.sql 规范: {entry.name}"
                )
            version, description = match.group(1), match.group(2)
            migrations.append(
                Migration(
                    version=version,
                    description=description,
                    path=entry.resolve(),
                )
            )

        if not migrations:
            return []

        # 校验版本号连续且从 0001 开始
        versions = [int(m.version) for m in migrations]
        expected = list(range(1, len(versions) + 1))
        if versions != expected:
            raise MigrationError(
                "迁移版本号不连续或未从 0001 开始: "
                f"实际 {[m.version for m in migrations]}"
            )
        return migrations

    def applied_versions(self) -> dict[str, str]:
        """返回 ``{version: checksum}``，已应用迁移的校验和记录。

        若数据库或 ``schema_migrations`` 表不存在，会自动创建（空记录）。
        """
        self._ensure_parent_dir()
        with self._connect() as conn:
            self._ensure_schema_migrations(conn)
            cur = conn.execute(
                "SELECT version, checksum FROM schema_migrations ORDER BY version"
            )
            rows = cur.fetchall()
            cur.close()
        return {row[0]: row[1] for row in rows}

    def run(self) -> list[str]:
        """应用所有待应用迁移，返回本次实际应用的版本号列表。

        - 已应用迁移：校验 checksum，不匹配则 ``MigrationError``；
        - 未应用迁移：在事务中执行 SQL，写入 ``schema_migrations``；
        - 无待应用迁移时返回空列表。

        任何迁移失败立即抛出 ``MigrationError``，不继续后续迁移。
        """
        self._ensure_parent_dir()
        migrations = self.discover()
        applied_now: list[str] = []

        with self._connect() as conn:
            self._ensure_schema_migrations(conn)
            recorded = self._load_recorded(conn)

            for migration in migrations:
                if migration.version in recorded:
                    recorded_checksum = recorded[migration.version]
                    if migration.checksum != recorded_checksum:
                        raise MigrationError(
                            f"迁移 {migration.filename} 校验和冲突："
                            f"记录 {recorded_checksum}，实际 {migration.checksum}。"
                            "已发布的迁移不得修改；如需变更请新增迁移。"
                        )
                    continue

                self._apply_migration(conn, migration)
                applied_now.append(migration.version)

        return applied_now

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _connect(self) -> sqlite3.Connection:
        """打开连接并应用 PRAGMA 基线（autocommit 模式，由调用方管理事务）。"""
        conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        try:
            for stmt in _PRAGMA_STATEMENTS:
                conn.execute(stmt)
        except Exception:
            conn.close()
            raise
        return conn

    def _ensure_parent_dir(self) -> None:
        """确保数据库父目录存在。"""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def _ensure_schema_migrations(self, conn: sqlite3.Connection) -> None:
        """创建 schema_migrations 表（若不存在）。"""
        conn.execute(_SCHEMA_MIGRATIONS_DDL)

    def _load_recorded(self, conn: sqlite3.Connection) -> dict[str, str]:
        """读取已记录的迁移 ``{version: checksum}``。"""
        cur = conn.execute(
            "SELECT version, checksum FROM schema_migrations"
        )
        rows = cur.fetchall()
        cur.close()
        return {row[0]: row[1] for row in rows}

    def _apply_migration(
        self, conn: sqlite3.Connection, migration: Migration
    ) -> None:
        """在单事务中执行迁移 SQL 并记录到 schema_migrations。

        迁移 SQL 与 ``schema_migrations`` 记录写入同一事务，保证原子性：
        SQL 失败则记录也不写入，整迁移回滚。

        实现说明：``executescript`` 在 ``isolation_level=None`` 下不会隐式
        COMMIT，因此将 ``BEGIN IMMEDIATE`` / ``COMMIT`` 显式写入脚本，使
        迁移 SQL 与记录 INSERT 处于同一事务。迁移脚本禁止包含事务控制语句。
        """
        applied_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # INSERT 值均为受控来源（版本号、文件名描述、SHA-256、时间戳），非用户输入；
        # 仍对单引号做防御性转义。description 受 [a-z0-9_]+ 正则约束，不含引号。
        def _esc(value: str) -> str:
            return value.replace("'", "''")

        insert_sql = (
            "INSERT INTO schema_migrations "
            "(version, description, checksum, applied_at, filename) "
            f"VALUES ('{_esc(migration.version)}', "
            f"'{_esc(migration.description)}', "
            f"'{_esc(migration.checksum)}', "
            f"'{_esc(applied_at)}', "
            f"'{_esc(migration.filename)}');"
        )

        script = (
            "BEGIN IMMEDIATE;\n"
            f"{migration.sql}\n"
            f"{insert_sql}\n"
            "COMMIT;"
        )

        try:
            conn.executescript(script)
        except Exception as exc:
            # 脚本在 COMMIT 前失败时事务可能仍开启；尝试回滚以保证不留半写入。
            # 若事务已结束（如 BEGIN 前失败），ROLLBACK 会抛 OperationalError，忽略。
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise MigrationError(
                f"迁移 {migration.filename} 执行失败: {exc}"
            ) from exc


# --------------------------------------------------------------------------- #
# CLI 入口（供 scripts/init-db.ps1 调用）
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    """命令行入口：应用迁移并返回退出码。"""
    parser = argparse.ArgumentParser(
        prog="migrations.runner",
        description="应用 maf.db 顺序 SQL 迁移。",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=Path("data/maf.db"),
        help="目标 SQLite 数据库路径（默认 data/maf.db）",
    )
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=Path("migrations"),
        help="迁移脚本目录（默认 migrations）",
    )
    args = parser.parse_args(argv)

    runner = MigrationRunner(
        migrations_dir=args.migrations_dir,
        db_path=args.db_path,
    )
    try:
        applied = runner.run()
    except MigrationError as exc:
        print(f"[migrations] 错误: {exc}", file=sys.stderr)
        return 1

    if applied:
        print(f"[migrations] 已应用 {len(applied)} 个迁移: {applied}")
    else:
        print("[migrations] 无待应用迁移，已是最新版本。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
