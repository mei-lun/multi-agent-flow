"""Tool 定义、MCP 映射与 Tool Registry 持久化接口。

TASK-048 扩展：
- 保留原有 ``ToolRepository`` Protocol（其他任务接口契约，TASK-049 范围）；
- 新增 ``TOOL_REGISTRY_SCHEMA_SQL`` 与 ``init_tool_registry_schema``，定义
  ``tools`` 表并供测试与首次启动建表使用；
- 新增 ``ToolRecord`` dataclass 与 ``SqliteToolRegistryRepository``，提供
  Tool Registry 的 CRUD 方法（按 name+version 查询、列表、版本列表、删除）。

表结构（``tools``）：
- ``id`` TEXT PRIMARY KEY：UUID；
- ``name`` TEXT NOT NULL：Tool 业务名；
- ``version`` TEXT NOT NULL：语义版本字符串；
- ``description`` TEXT NOT NULL DEFAULT ''；
- ``adapter_type`` TEXT NOT NULL DEFAULT 'NATIVE'；
- ``input_schema`` TEXT NOT NULL CHECK(json_valid)；
- ``output_schema`` TEXT NOT NULL CHECK(json_valid)；
- ``capabilities`` TEXT NOT NULL CHECK(json_valid)；
- ``created_at`` TEXT NOT NULL：RFC 3339 时间戳；
- ``created_by`` TEXT NOT NULL：注册者 user_id；
- ``version_no`` INTEGER NOT NULL：按 name 内部自增序号；
- ``UNIQUE(name, version)``：业务唯一键。

事务边界：repository 方法接受 ``aiosqlite.Connection``（由
``SqliteUnitOfWork`` 提供），不自开事务；service 层负责事务边界。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import aiosqlite

from .schemas import McpServerView, ToolRegistrationView, ToolVersionView

# --------------------------------------------------------------------------- #
# tools 表 DDL（供测试与首次启动建表使用；正式部署由 migrations 负责）
# --------------------------------------------------------------------------- #

TOOL_REGISTRY_SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS tools (
    id            TEXT    PRIMARY KEY,
    name          TEXT    NOT NULL,
    version       TEXT    NOT NULL,
    description   TEXT    NOT NULL DEFAULT '',
    adapter_type  TEXT    NOT NULL DEFAULT 'NATIVE',
    input_schema  TEXT    NOT NULL CHECK(json_valid(input_schema)),
    output_schema TEXT    NOT NULL CHECK(json_valid(output_schema)),
    capabilities  TEXT    NOT NULL CHECK(json_valid(capabilities)),
    created_at    TEXT    NOT NULL,
    created_by    TEXT    NOT NULL,
    version_no    INTEGER NOT NULL,
    UNIQUE(name, version)
);

CREATE INDEX IF NOT EXISTS idx_tools_name ON tools(name);
CREATE INDEX IF NOT EXISTS idx_tools_name_version_no ON tools(name, version_no);
"""


async def init_tool_registry_schema(conn: aiosqlite.Connection) -> None:
    """在给定连接上创建 ``tools`` 表与索引（``CREATE ... IF NOT EXISTS``，幂等）。

    正式部署由 ``migrations/`` 顺序迁移负责；本函数供测试与开发期首次启动使用。

    实现说明：使用逐条 ``execute`` 而非 ``executescript``。``executescript`` 会
    在执行前隐式 COMMIT 当前事务（Python sqlite3 文档行为），这会破坏
    ``Database.write_connection`` 的 ``BEGIN IMMEDIATE``/``COMMIT`` 事务边界。
    """
    for raw in TOOL_REGISTRY_SCHEMA_SQL.split(";"):
        stmt = raw.strip()
        if stmt:
            await conn.execute(stmt)


# --------------------------------------------------------------------------- #
# 行映射 dataclass
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolRecord:
    """``tools`` 表行映射，供 service 层内部使用。

    ``input_schema`` / ``output_schema`` / ``capabilities`` 在持久化层以 JSON
    TEXT 存储，本 dataclass 持有已反序列化的 Python 对象。
    """

    id: str
    name: str
    version: str
    description: str
    adapter_type: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    capabilities: list[str]
    created_at: str
    created_by: str
    version_no: int


def _row_to_record(row: aiosqlite.Row | tuple) -> ToolRecord:
    """把 ``tools`` 表行映射为 ``ToolRecord``。"""
    if isinstance(row, aiosqlite.Row):
        row = tuple(row)
    return ToolRecord(
        id=str(row[0]),
        name=str(row[1]),
        version=str(row[2]),
        description=str(row[3]),
        adapter_type=str(row[4]),
        input_schema=json.loads(row[5]) if row[5] else {},
        output_schema=json.loads(row[6]) if row[6] else {},
        capabilities=json.loads(row[7]) if row[7] else [],
        created_at=str(row[8]),
        created_by=str(row[9]),
        version_no=int(row[10]),
    )


def record_to_view(record: ToolRecord) -> ToolRegistrationView:
    """把 ``ToolRecord`` 映射为对外 ``ToolRegistrationView``。"""
    return ToolRegistrationView(
        id=record.id,
        name=record.name,
        version=record.version,
        description=record.description,
        adapter_type=record.adapter_type,
        input_schema=dict(record.input_schema),
        output_schema=dict(record.output_schema),
        capabilities=list(record.capabilities),
        version_no=record.version_no,
        created_at=record.created_at,
        created_by=record.created_by,
    )


def record_to_version_view(record: ToolRecord) -> ToolVersionView:
    """把 ``ToolRecord`` 映射为 ``ToolVersionView``（版本列表条目）。"""
    return ToolVersionView(
        version=record.version,
        version_no=record.version_no,
        description=record.description,
        adapter_type=record.adapter_type,
        created_at=record.created_at,
        created_by=record.created_by,
    )


_TOOL_COLUMNS: str = (
    "id, name, version, description, adapter_type, "
    "input_schema, output_schema, capabilities, "
    "created_at, created_by, version_no"
)


# --------------------------------------------------------------------------- #
# 原有 Protocol（保留 TASK-049 接口契约）
# --------------------------------------------------------------------------- #


class ToolRepository(Protocol):
    async def get_by_key(self, key: str, version: int | None = None) -> ToolRegistrationView | None:
        """按稳定 key 和可选精确 version 查询；运行时必须传 version。"""
        ...

    async def save(self, tool: ToolRegistrationView) -> ToolRegistrationView:
        """保存新版本或状态；Schema/adapter/risk 变化必须生成版本。"""
        ...

    async def list_by_mcp_server(self, mcp_server_id: str) -> list[ToolRegistrationView]:
        """返回该 Server 同步产生的当前 Tool，用于差异计算。"""
        ...

    async def disable_missing_mcp_tools(self, mcp_server_id: str, present_keys: set[str]) -> list[str]:
        """把远端缺失的当前版本标 DISABLED，返回受影响 ID；保留历史。"""
        ...


# --------------------------------------------------------------------------- #
# TASK-048: Tool Registry SQLite 实现
# --------------------------------------------------------------------------- #


class SqliteToolRegistryRepository:
    """``tools`` 表的 SQLite 仓储实现，用于 Tool Registry CRUD。

    所有方法接受 ``aiosqlite.Connection``（由 ``SqliteUnitOfWork`` 提供），
    不自开事务、不调用网络、不写日志。

    谁调用它：
        ``ToolRegistryService`` 在 ``SqliteUnitOfWork`` 事务内调用本类方法
        读写 ``tools`` 表。

    安全约束：
        - ``input_schema`` / ``output_schema`` / ``capabilities`` 在写入前
          ``json.dumps`` 序列化为 TEXT，由 ``json_valid`` CHECK 保证；
        - 同名同版本重复插入由 ``UNIQUE(name, version)`` 约束拒绝；
        - ``version_no`` 由 ``next_version_no`` 计算后传入，不在 SQL 内自增，
          保证 service 层可对计算结果做断言。
    """

    async def get_by_name_version(
        self,
        conn: aiosqlite.Connection,
        name: str,
        version: str,
    ) -> ToolRecord | None:
        """按 ``name + version`` 查询；不存在返回 None。"""
        sql = f"SELECT {_TOOL_COLUMNS} FROM tools WHERE name = ? AND version = ? LIMIT 1"
        async with conn.execute(sql, (name, version)) as cur:
            row = await cur.fetchone()
        return _row_to_record(row) if row is not None else None

    async def list_all(self, conn: aiosqlite.Connection) -> list[ToolRecord]:
        """按 ``name``、``version_no`` 升序列出全部 Tool。"""
        sql = f"SELECT {_TOOL_COLUMNS} FROM tools ORDER BY name ASC, version_no ASC"
        async with conn.execute(sql) as cur:
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def list_versions_by_name(
        self,
        conn: aiosqlite.Connection,
        name: str,
    ) -> list[ToolRecord]:
        """列出指定 ``name`` 的全部版本，按 ``version_no`` 升序。"""
        sql = (
            f"SELECT {_TOOL_COLUMNS} FROM tools WHERE name = ? "
            f"ORDER BY version_no ASC"
        )
        async with conn.execute(sql, (name,)) as cur:
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def next_version_no(
        self,
        conn: aiosqlite.Connection,
        name: str,
    ) -> int:
        """返回 ``name`` 下一个 ``version_no``；无历史则返回 1。"""
        sql = "SELECT COALESCE(MAX(version_no), 0) + 1 FROM tools WHERE name = ?"
        async with conn.execute(sql, (name,)) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row is not None else 1

    async def insert(
        self,
        conn: aiosqlite.Connection,
        record: ToolRecord,
    ) -> None:
        """插入一条 Tool 注册记录。

        ``UNIQUE(name, version)`` 冲突时抛 ``sqlite3.IntegrityError``；
        service 层捕获后转换为 ``AlreadyExistsError``。
        """
        await conn.execute(
            "INSERT INTO tools (id, name, version, description, adapter_type, "
            "input_schema, output_schema, capabilities, created_at, created_by, "
            "version_no) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.name,
                record.version,
                record.description,
                record.adapter_type,
                json.dumps(record.input_schema, ensure_ascii=False, default=str),
                json.dumps(record.output_schema, ensure_ascii=False, default=str),
                json.dumps(record.capabilities, ensure_ascii=False, default=str),
                record.created_at,
                record.created_by,
                record.version_no,
            ),
        )

    async def delete_by_name_version(
        self,
        conn: aiosqlite.Connection,
        name: str,
        version: str,
    ) -> bool:
        """按 ``name + version`` 删除；返回是否命中一行。

        保留同名其他版本；仅删除指定版本。``UNIQUE(name, version)`` 保证
        至多一行被删。
        """
        cursor = await conn.execute(
            "DELETE FROM tools WHERE name = ? AND version = ?",
            (name, version),
        )
        try:
            rowcount = cursor.rowcount
        finally:
            await cursor.close()
        return rowcount > 0


def new_tool_id() -> str:
    """生成新 Tool 注册记录 ID（UUID v4 字符串）。"""
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- #
# TASK-049: mcp_servers 表 DDL 与 CRUD
# --------------------------------------------------------------------------- #

#: ``mcp_servers`` 表 DDL（供测试与首次启动建表；正式部署由 migrations 负责）。
#:
#: 表结构：
#: - ``url`` TEXT PRIMARY KEY：MCP 服务器 endpoint（自然主键）；
#: - ``name`` TEXT NOT NULL DEFAULT ''：展示名（从 url host 推导）；
#: - ``credential_secret_id`` TEXT：凭据 SecretService 引用 ID（无明文）；
#: - ``last_synced_at`` TEXT：上次同步 RFC 3339 时间戳，未同步为 NULL；
#: - ``synced_by`` TEXT NOT NULL DEFAULT ''：上次同步执行者 user_id；
#: - ``version_no`` INTEGER NOT NULL DEFAULT 1：配置版本号，upsert 自增。
MCP_SERVERS_SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS mcp_servers (
    url                   TEXT    PRIMARY KEY,
    name                  TEXT    NOT NULL DEFAULT '',
    credential_secret_id  TEXT,
    last_synced_at        TEXT,
    synced_by             TEXT    NOT NULL DEFAULT '',
    version_no            INTEGER NOT NULL DEFAULT 1
);
"""


async def init_mcp_servers_schema(conn: aiosqlite.Connection) -> None:
    """在给定连接上创建 ``mcp_servers`` 表（``CREATE ... IF NOT EXISTS``，幂等）。

    与 ``init_tool_registry_schema`` 一致，使用逐条 ``execute`` 而非
    ``executescript``，避免破坏 ``Database.write_connection`` 事务边界。
    """
    for raw in MCP_SERVERS_SCHEMA_SQL.split(";"):
        stmt = raw.strip()
        if stmt:
            await conn.execute(stmt)


@dataclass(frozen=True)
class McpServerRecord:
    """``mcp_servers`` 表行映射。

    ``credential_secret_id`` 仅持有 SecretService 引用，**绝不存明文**；
    ``last_synced_at`` 为 ``None`` 表示尚未同步。
    """

    url: str
    name: str
    credential_secret_id: str | None
    last_synced_at: str | None
    synced_by: str
    version_no: int


_MCP_SERVER_COLUMNS: str = (
    "url, name, credential_secret_id, last_synced_at, synced_by, version_no"
)


def _row_to_mcp_server(row: aiosqlite.Row | tuple) -> McpServerRecord:
    """把 ``mcp_servers`` 表行映射为 ``McpServerRecord``。"""
    if isinstance(row, aiosqlite.Row):
        row = tuple(row)
    return McpServerRecord(
        url=str(row[0]),
        name=str(row[1]),
        credential_secret_id=str(row[2]) if row[2] is not None else None,
        last_synced_at=str(row[3]) if row[3] is not None else None,
        synced_by=str(row[4]),
        version_no=int(row[5]),
    )


class SqliteMcpServerRepository:
    """``mcp_servers`` 表的 SQLite 仓储实现，用于 MCP 服务器配置 CRUD。

    所有方法接受 ``aiosqlite.Connection``（由 ``SqliteUnitOfWork`` 提供），
    不自开事务、不调用网络、不写日志、不解析凭据明文。

    谁调用它：
        ``McpToolSyncService`` 在 ``SqliteUnitOfWork`` 事务内调用本类方法
        读写 ``mcp_servers`` 表。

    安全约束：
        - ``credential_secret_id`` 只存引用 ID，明文由 ``SecretService`` 管理；
        - ``url`` 作为自然主键，upsert 时按 url 命中即更新；
        - ``version_no`` 由 ``next_version_no`` 计算后传入，不在 SQL 内自增。
    """

    async def get_by_url(
        self,
        conn: aiosqlite.Connection,
        url: str,
    ) -> McpServerRecord | None:
        """按 ``url`` 查询；不存在返回 None。"""
        sql = (
            f"SELECT {_MCP_SERVER_COLUMNS} FROM mcp_servers WHERE url = ? LIMIT 1"
        )
        async with conn.execute(sql, (url,)) as cur:
            row = await cur.fetchone()
        return _row_to_mcp_server(row) if row is not None else None

    async def list_all(self, conn: aiosqlite.Connection) -> list[McpServerRecord]:
        """按 ``url`` 升序列出全部 MCP 服务器配置。"""
        sql = (
            f"SELECT {_MCP_SERVER_COLUMNS} FROM mcp_servers ORDER BY url ASC"
        )
        async with conn.execute(sql) as cur:
            rows = await cur.fetchall()
        return [_row_to_mcp_server(r) for r in rows]

    async def next_version_no(
        self,
        conn: aiosqlite.Connection,
        url: str,
    ) -> int:
        """返回 ``url`` 下一个 ``version_no``；不存在返回 1。"""
        sql = (
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM mcp_servers WHERE url = ?"
        )
        async with conn.execute(sql, (url,)) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row is not None else 1

    async def upsert(
        self,
        conn: aiosqlite.Connection,
        record: McpServerRecord,
    ) -> None:
        """插入或更新 MCP 服务器配置。

        使用 ``INSERT ... ON CONFLICT(url) DO UPDATE`` 实现 upsert：
        - url 不存在 → INSERT；
        - url 已存在 → UPDATE name / credential_secret_id / last_synced_at /
          synced_by / version_no。
        """
        await conn.execute(
            "INSERT INTO mcp_servers (url, name, credential_secret_id, "
            "last_synced_at, synced_by, version_no) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET "
            "name = excluded.name, "
            "credential_secret_id = excluded.credential_secret_id, "
            "last_synced_at = excluded.last_synced_at, "
            "synced_by = excluded.synced_by, "
            "version_no = excluded.version_no",
            (
                record.url,
                record.name,
                record.credential_secret_id,
                record.last_synced_at,
                record.synced_by,
                record.version_no,
            ),
        )

    async def delete_by_url(
        self,
        conn: aiosqlite.Connection,
        url: str,
    ) -> bool:
        """按 ``url`` 删除；返回是否命中一行。"""
        cursor = await conn.execute(
            "DELETE FROM mcp_servers WHERE url = ?",
            (url,),
        )
        try:
            rowcount = cursor.rowcount
        finally:
            await cursor.close()
        return rowcount > 0


def mcp_server_to_view(record: McpServerRecord) -> McpServerView:
    """把 ``McpServerRecord`` 映射为对外 ``McpServerView``。"""
    # 延迟导入避免循环引用（schemas 不在本模块导入链顶部）
    from .schemas import McpServerView

    return McpServerView(
        url=record.url,
        name=record.name,
        credential_secret_id=record.credential_secret_id,
        last_synced_at=record.last_synced_at,
        synced_by=record.synced_by,
        version_no=record.version_no,
    )


__all__ = [
    "MCP_SERVERS_SCHEMA_SQL",
    "McpServerRecord",
    "SqliteMcpServerRepository",
    "SqliteToolRegistryRepository",
    "TOOL_REGISTRY_SCHEMA_SQL",
    "ToolRecord",
    "ToolRepository",
    "init_mcp_servers_schema",
    "init_tool_registry_schema",
    "mcp_server_to_view",
    "new_tool_id",
    "record_to_view",
    "record_to_version_view",
]
