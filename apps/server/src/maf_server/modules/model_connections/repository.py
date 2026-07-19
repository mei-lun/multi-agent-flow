"""模型连接持久化接口与 SQLite 实现。

TASK-037 范围：
- 定义 ``model_connections`` 表 DDL（幂等 ``CREATE TABLE IF NOT EXISTS``），
- 提供 ``SqliteModelConnectionRepository`` 负责该表的 CRUD；
- 凭据明文绝不进入本表：``credential_secret_id`` 为 SecretService 返回的 opaque
  引用，``credential_fingerprint`` 为不可逆指纹（``sha256(plaintext)[:8] + ".." + plaintext[-4:]``）。

表结构（对应任务说明）：
    id / name / provider / model_id / api_base / credential_type /
    credential_secret_id / credential_fingerprint / status /
    created_by / created_at / updated_at / version_no

事务边界：repository 方法接受 ``aiosqlite.Connection``（由 ``SqliteUnitOfWork``
提供），不自开事务；service 层负责 ``BEGIN IMMEDIATE``/``COMMIT``。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import aiosqlite

from maf_domain.errors import NotFoundError, VersionConflictError

from .schemas import STATUS_UNVERIFIED

# --------------------------------------------------------------------------- #
# 表结构 DDL（供测试与首次启动建表使用；正式部署由 migrations 负责）
# --------------------------------------------------------------------------- #

SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS model_connections (
    id                       TEXT    PRIMARY KEY,
    name                     TEXT    NOT NULL,
    provider                 TEXT    NOT NULL,
    model_id                 TEXT    NOT NULL,
    api_base                 TEXT    NOT NULL,
    credential_type          TEXT    NOT NULL,
    credential_secret_id     TEXT    NOT NULL,
    credential_fingerprint   TEXT    NOT NULL,
    status                   TEXT    NOT NULL DEFAULT 'UNVERIFIED',
    created_by               TEXT    NOT NULL,
    created_at               TEXT    NOT NULL,
    updated_at               TEXT    NOT NULL,
    version_no               INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_model_connections_created_by
    ON model_connections(created_by);
"""


async def init_schema(conn: aiosqlite.Connection) -> None:
    """在给定连接上创建 ``model_connections`` 表（``CREATE TABLE IF NOT EXISTS``，幂等）。

    正式部署由 ``migrations/`` 顺序迁移负责；本函数供测试与开发期首次启动使用。

    实现说明：使用逐条 ``execute`` 而非 ``executescript``，因为
    ``executescript`` 会隐式 COMMIT 当前事务，与 ``write_connection`` 的
    ``BEGIN IMMEDIATE``/``COMMIT`` 边界冲突。
    """
    for raw in SCHEMA_SQL.split(";"):
        stmt = raw.strip()
        if stmt:
            await conn.execute(stmt)


# --------------------------------------------------------------------------- #
# 行映射 dataclass
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelConnectionRecord:
    """``model_connections`` 表行映射，供 service 层内部使用。

    安全约束：``credential_secret_id`` 是 opaque 引用，``credential_fingerprint``
    是不可逆指纹；两者均不含明文。明文凭据绝不进入本结构。
    """

    id: str
    name: str
    provider: str
    model_id: str
    api_base: str
    credential_type: str
    credential_secret_id: str
    credential_fingerprint: str
    status: str
    created_by: str
    created_at: str
    updated_at: str
    version_no: int = 1


# --------------------------------------------------------------------------- #
# SQLite 具体实现
# --------------------------------------------------------------------------- #


_CONNECTION_COLUMNS: str = (
    "id, name, provider, model_id, api_base, credential_type, "
    "credential_secret_id, credential_fingerprint, status, created_by, "
    "created_at, updated_at, version_no"
)


def _row_to_record(row: aiosqlite.Row | tuple) -> ModelConnectionRecord:
    """把 ``model_connections`` 表行映射为 ``ModelConnectionRecord``。"""
    if isinstance(row, aiosqlite.Row):
        row = tuple(row)
    return ModelConnectionRecord(
        id=str(row[0]),
        name=str(row[1]),
        provider=str(row[2]),
        model_id=str(row[3]),
        api_base=str(row[4]),
        credential_type=str(row[5]),
        credential_secret_id=str(row[6]),
        credential_fingerprint=str(row[7]),
        status=str(row[8]),
        created_by=str(row[9]),
        created_at=str(row[10]),
        updated_at=str(row[11]),
        version_no=int(row[12]),
    )


class SqliteModelConnectionRepository:
    """``model_connections`` 表的 SQLite 仓储实现。

    所有方法接受 ``aiosqlite.Connection``（由 ``SqliteUnitOfWork`` 提供），
    不自开事务、不调用网络、不写日志。凭据明文绝不进入本类任何方法的输入或输出；
    本类只处理 ``credential_secret_id`` 引用与不可逆 ``credential_fingerprint``。

    谁调用它：
        ``ModelConnectionServiceImpl`` 在 ``SqliteUnitOfWork`` 事务内调用本类方法
        读写数据。
    """

    async def get_by_id(
        self, conn: aiosqlite.Connection, connection_id: str
    ) -> ModelConnectionRecord | None:
        """按 id 查询连接；不存在返回 None。"""
        sql = f"SELECT {_CONNECTION_COLUMNS} FROM model_connections WHERE id = ? LIMIT 1"
        async with conn.execute(sql, (connection_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_record(row) if row is not None else None

    async def list_all(
        self, conn: aiosqlite.Connection
    ) -> list[ModelConnectionRecord]:
        """列出所有连接，按 ``created_at`` 升序稳定排序。"""
        sql = f"SELECT {_CONNECTION_COLUMNS} FROM model_connections ORDER BY created_at ASC, id ASC"
        async with conn.execute(sql) as cur:
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def insert(
        self,
        conn: aiosqlite.Connection,
        *,
        connection_id: str,
        name: str,
        provider: str,
        model_id: str,
        api_base: str,
        credential_type: str,
        credential_secret_id: str,
        credential_fingerprint: str,
        created_by: str,
        created_at: str,
    ) -> None:
        """插入一行 model_connections；version_no 从 1 开始，status 为 UNVERIFIED。

        调用方应确保 ``connection_id`` 唯一。重复 id 触发 ``IntegrityError``，
        由事务回滚。
        """
        await conn.execute(
            "INSERT INTO model_connections "
            "(id, name, provider, model_id, api_base, credential_type, "
            "credential_secret_id, credential_fingerprint, status, created_by, "
            "created_at, updated_at, version_no) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                connection_id,
                name,
                provider,
                model_id,
                api_base,
                credential_type,
                credential_secret_id,
                credential_fingerprint,
                STATUS_UNVERIFIED,
                created_by,
                created_at,
                created_at,
            ),
        )

    async def delete_with_expected_version(
        self,
        conn: aiosqlite.Connection,
        connection_id: str,
        expected_version: int,
    ) -> None:
        """按 ``id`` 与 ``version_no`` 删除连接（乐观锁）。

        影响行数 0 时：若行不存在抛 ``NotFoundError``；若存在但版本不匹配抛
        ``VersionConflictError``。
        """
        cursor = await conn.execute(
            "DELETE FROM model_connections WHERE id = ? AND version_no = ?",
            (connection_id, expected_version),
        )
        try:
            rowcount = cursor.rowcount
        finally:
            await cursor.close()

        if rowcount > 0:
            return

        existing = await self.get_by_id(conn, connection_id)
        if existing is None:
            raise NotFoundError(
                "模型连接不存在",
                context={"connection_id": connection_id},
            )
        raise VersionConflictError(
            "模型连接版本不匹配",
            context={
                "connection_id": connection_id,
                "expected": expected_version,
                "actual": existing.version_no,
            },
            retryable=True,
        )

    async def update_status(
        self,
        conn: aiosqlite.Connection,
        connection_id: str,
        status: str,
        updated_at: str,
    ) -> bool:
        """更新 ``status`` 与 ``updated_at``；不递增 ``version_no``。

        ``test_connection`` 是验证动作而非配置变更，不触发乐观锁版本递增
        （与 ``users.last_login_at`` 更新模式一致）。返回是否命中一行。
        """
        cursor = await conn.execute(
            "UPDATE model_connections SET status = ?, updated_at = ? WHERE id = ?",
            (status, updated_at, connection_id),
        )
        try:
            rowcount = cursor.rowcount
        finally:
            await cursor.close()
        return rowcount > 0


def new_connection_id() -> str:
    """生成新模型连接 ID（UUID v4 字符串）。"""
    return str(uuid.uuid4())


__all__ = [
    "SCHEMA_SQL",
    "ModelConnectionRecord",
    "SqliteModelConnectionRepository",
    "init_schema",
    "new_connection_id",
]
