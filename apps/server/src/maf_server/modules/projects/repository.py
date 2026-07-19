"""Project 模块持久化接口与 SQLite 实现。

TASK-033 范围：
- ``SqliteProjectRepository`` 负责 ``projects`` 与 ``project_members`` 两张表的 CRUD。
- 方法接受 ``aiosqlite.Connection``（由 ``SqliteUnitOfWork`` 提供），不自开事务。
- 软删除：``delete_project`` 在 service 层设置 ``deleted_at``；``get``/``list`` 默认
  过滤 ``deleted_at IS NULL``。``get_include_deleted`` 供内部判断存在性使用。
- 乐观锁：``update``/``delete``/``update_member`` 由 service 层调用
  ``update_with_expected_version``；本 repository 仅提供读与插入。

事务边界：repository 方法不自开事务，由 service 层负责 ``BEGIN IMMEDIATE``/``COMMIT``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

import aiosqlite

from .schemas import (
    ChangeRequestView,
    ProjectInputView,
    ProjectMemberRole,
    ProjectMemberView,
    ProjectStatus,
    ProjectView,
)


PROJECT_EXTENSIONS_DDL = """
CREATE TABLE IF NOT EXISTS project_input_versions (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, version_no INTEGER NOT NULL,
    name TEXT NOT NULL, content_type TEXT NOT NULL, artifact_version_id TEXT NOT NULL,
    change_summary TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL, UNIQUE(project_id, version_no), UNIQUE(project_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS project_change_requests (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
    status TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
    affected_requirement_ids TEXT NOT NULL, requested_action TEXT NOT NULL,
    inbox_item_id TEXT, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL, UNIQUE(project_id, idempotency_key)
);
"""


async def init_project_extensions_schema(conn: aiosqlite.Connection) -> None:
    for raw in PROJECT_EXTENSIONS_DDL.split(";"):
        if raw.strip():
            await conn.execute(raw.strip())

# --------------------------------------------------------------------------- #
# 列名常量（用于 SELECT 拼接，避免列名漂移）
# --------------------------------------------------------------------------- #

_PROJECT_COLUMNS = (
    "id, name, description, status, created_at, created_by, "
    "updated_at, version_no, deleted_at"
)

_MEMBER_COLUMNS = (
    "project_id, user_id, role, added_at, added_by, version_no"
)


# --------------------------------------------------------------------------- #
# 行映射 dataclass（供 service 层内部使用，不直接对外暴露）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProjectRecord:
    """``projects`` 表行映射。``deleted_at`` 非空表示已软删除。"""

    id: str
    name: str
    description: str
    status: ProjectStatus
    created_at: str
    created_by: str
    updated_at: str
    version_no: int
    deleted_at: str | None = None


@dataclass(frozen=True)
class MemberRecord:
    """``project_members`` 表行映射。"""

    project_id: str
    user_id: str
    role: ProjectMemberRole
    added_at: str
    added_by: str
    version_no: int


# --------------------------------------------------------------------------- #
# 行映射辅助函数
# --------------------------------------------------------------------------- #


def _row_to_project(row: aiosqlite.Row | tuple | None) -> ProjectRecord | None:
    """把 ``projects`` 表行映射为 ``ProjectRecord``；``None`` 输入返回 ``None``。"""
    if row is None:
        return None
    if isinstance(row, aiosqlite.Row):
        row = tuple(row)
    return ProjectRecord(
        id=str(row[0]),
        name=str(row[1]),
        description=str(row[2]),
        status=str(row[3]),  # type: ignore[arg-type]
        created_at=str(row[4]),
        created_by=str(row[5]),
        updated_at=str(row[6]),
        version_no=int(row[7]),
        deleted_at=str(row[8]) if row[8] is not None else None,
    )


def _row_to_member(row: aiosqlite.Row | tuple | None) -> MemberRecord | None:
    """把 ``project_members`` 表行映射为 ``MemberRecord``；``None`` 输入返回 ``None``。"""
    if row is None:
        return None
    if isinstance(row, aiosqlite.Row):
        row = tuple(row)
    return MemberRecord(
        project_id=str(row[0]),
        user_id=str(row[1]),
        role=str(row[2]),  # type: ignore[arg-type]
        added_at=str(row[3]),
        added_by=str(row[4]),
        version_no=int(row[5]),
    )


def project_record_to_view(record: ProjectRecord) -> ProjectView:
    """``ProjectRecord`` → ``ProjectView``（对外视图）。"""
    return ProjectView(
        id=record.id,
        name=record.name,
        description=record.description,
        status=record.status,
        created_at=record.created_at,
        created_by=record.created_by,
        updated_at=record.updated_at,
        version=record.version_no,
        deleted_at=record.deleted_at,
    )


def member_record_to_view(record: MemberRecord) -> ProjectMemberView:
    """``MemberRecord`` → ``ProjectMemberView``（对外视图）。"""
    return ProjectMemberView(
        project_id=record.project_id,
        user_id=record.user_id,
        role=record.role,
        added_at=record.added_at,
        added_by=record.added_by,
        version=record.version_no,
    )


# --------------------------------------------------------------------------- #
# Protocol（保留接口契约）
# --------------------------------------------------------------------------- #


class ProjectRepository(Protocol):
    """Project 持久化接口契约（供后续任务扩展）。"""

    async def get_project(
        self, conn: aiosqlite.Connection, project_id: str
    ) -> ProjectRecord | None:
        """按 ID 读取未软删除的项目；不存在返回 ``None``。"""
        ...

    async def list_projects_by_member(
        self, conn: aiosqlite.Connection, user_id: str
    ) -> list[ProjectRecord]:
        """返回 ``user_id`` 作为成员的未软删除项目列表。"""
        ...


# --------------------------------------------------------------------------- #
# SQLite 具体实现
# --------------------------------------------------------------------------- #


class SqliteProjectRepository:
    """``ProjectRepository`` 的 SQLite 实现。

    所有方法接受 ``aiosqlite.Connection``（由 ``SqliteUnitOfWork`` 提供），
    不自开事务，由 service 层负责事务边界。
    """

    # ------------------------------------------------------------------ #
    # projects 表
    # ------------------------------------------------------------------ #

    async def get_project(
        self, conn: aiosqlite.Connection, project_id: str
    ) -> ProjectRecord | None:
        """按 ID 读取未软删除的项目；不存在或已软删除返回 ``None``。"""
        sql = (
            f"SELECT {_PROJECT_COLUMNS} FROM projects "
            "WHERE id = ? AND deleted_at IS NULL LIMIT 1"
        )
        async with conn.execute(sql, (project_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_project(row)

    async def get_project_include_deleted(
        self, conn: aiosqlite.Connection, project_id: str
    ) -> ProjectRecord | None:
        """按 ID 读取项目（含已软删除）；用于存在性判断。"""
        sql = f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE id = ? LIMIT 1"
        async with conn.execute(sql, (project_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_project(row)

    async def list_projects_by_member(
        self, conn: aiosqlite.Connection, user_id: str
    ) -> list[ProjectRecord]:
        """返回 ``user_id`` 作为成员的未软删除项目列表，按 ``created_at`` 升序。

        使用 ``DISTINCT`` 避免重复（一个用户理论上在同一项目只有一条成员记录，
        但 ``DISTINCT`` 保证语义稳定）。
        """
        sql = (
            f"SELECT DISTINCT {_PROJECT_COLUMNS} FROM projects p "
            "WHERE p.deleted_at IS NULL "
            "AND EXISTS (SELECT 1 FROM project_members m "
            "            WHERE m.project_id = p.id AND m.user_id = ?) "
            "ORDER BY p.created_at ASC, p.id ASC"
        )
        async with conn.execute(sql, (user_id,)) as cur:
            rows = await cur.fetchall()
        return [r for r in (_row_to_project(row) for row in rows) if r is not None]

    async def insert_project(
        self, conn: aiosqlite.Connection, record: ProjectRecord
    ) -> None:
        """插入新项目行；``version_no`` 初始为 1，``deleted_at`` 为 ``None``。"""
        await conn.execute(
            "INSERT INTO projects (id, name, description, status, created_at, "
            "created_by, updated_at, version_no, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                record.id,
                record.name,
                record.description,
                record.status,
                record.created_at,
                record.created_by,
                record.updated_at,
                record.version_no,
            ),
        )

    # ------------------------------------------------------------------ #
    # project_members 表
    # ------------------------------------------------------------------ #

    async def get_member(
        self, conn: aiosqlite.Connection, project_id: str, user_id: str
    ) -> MemberRecord | None:
        """按 ``(project_id, user_id)`` 读取成员记录；不存在返回 ``None``。"""
        sql = (
            f"SELECT {_MEMBER_COLUMNS} FROM project_members "
            "WHERE project_id = ? AND user_id = ? LIMIT 1"
        )
        async with conn.execute(sql, (project_id, user_id)) as cur:
            row = await cur.fetchone()
        return _row_to_member(row)

    async def list_members(
        self, conn: aiosqlite.Connection, project_id: str
    ) -> list[MemberRecord]:
        """返回项目的全部成员，按 ``added_at`` 升序、``user_id`` 次序稳定排序。"""
        sql = (
            f"SELECT {_MEMBER_COLUMNS} FROM project_members "
            "WHERE project_id = ? ORDER BY added_at ASC, user_id ASC"
        )
        async with conn.execute(sql, (project_id,)) as cur:
            rows = await cur.fetchall()
        return [r for r in (_row_to_member(row) for row in rows) if r is not None]

    async def count_members_by_role(
        self,
        conn: aiosqlite.Connection,
        project_id: str,
        role: ProjectMemberRole,
    ) -> int:
        """统计项目中指定角色的成员数量；用于最后 OWNER 保护。"""
        sql = (
            "SELECT COUNT(*) FROM project_members "
            "WHERE project_id = ? AND role = ?"
        )
        async with conn.execute(sql, (project_id, role)) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row is not None else 0

    async def insert_member(
        self, conn: aiosqlite.Connection, record: MemberRecord
    ) -> None:
        """插入新成员行；``version_no`` 初始为 1。"""
        await conn.execute(
            "INSERT INTO project_members (project_id, user_id, role, added_at, "
            "added_by, version_no) VALUES (?, ?, ?, ?, ?, ?)",
            (
                record.project_id,
                record.user_id,
                record.role,
                record.added_at,
                record.added_by,
                record.version_no,
            ),
        )

    async def delete_member(
        self, conn: aiosqlite.Connection, project_id: str, user_id: str
    ) -> None:
        """删除成员行（物理删除，成员关系不可恢复）。"""
        await conn.execute(
            "DELETE FROM project_members WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        )

    async def get_input_by_idempotency_key(
        self, conn: aiosqlite.Connection, project_id: str, key: str
    ) -> tuple[ProjectInputView, str] | None:
        async with conn.execute(
            "SELECT id, project_id, version_no, name, content_type, artifact_version_id, "
            "change_summary, created_at, request_hash FROM project_input_versions "
            "WHERE project_id = ? AND idempotency_key = ?", (project_id, key)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return ({"id": row[0], "project_id": row[1], "version": row[2], "name": row[3],
                 "content_type": row[4], "artifact_version_id": row[5],
                 "change_summary": row[6], "created_at": row[7]}, row[8])

    async def get_input_version(
        self, conn: aiosqlite.Connection, input_id: str
    ) -> ProjectInputView | None:
        async with conn.execute(
            "SELECT id, project_id, version_no, name, content_type, artifact_version_id, "
            "change_summary, created_at FROM project_input_versions WHERE id = ?", (input_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return {"id": row[0], "project_id": row[1], "version": row[2], "name": row[3],
                "content_type": row[4], "artifact_version_id": row[5],
                "change_summary": row[6], "created_at": row[7]}

    async def append_input_version(
        self, conn: aiosqlite.Connection, *, item: ProjectInputView,
        idempotency_key: str, request_hash: str
    ) -> None:
        await conn.execute(
            "INSERT INTO project_input_versions (id, project_id, version_no, name, content_type, "
            "artifact_version_id, change_summary, idempotency_key, request_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item["id"], item["project_id"], item["version"], item["name"], item["content_type"],
             item["artifact_version_id"], item["change_summary"], idempotency_key, request_hash,
             item["created_at"]),
        )

    async def next_input_version(self, conn: aiosqlite.Connection, project_id: str) -> int:
        async with conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM project_input_versions WHERE project_id = ?",
            (project_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0])

    async def get_change_request_by_key(
        self, conn: aiosqlite.Connection, project_id: str, key: str
    ) -> tuple[ChangeRequestView, str] | None:
        async with conn.execute(
            "SELECT id, project_id, run_id, status, title, description, affected_requirement_ids, "
            "requested_action, inbox_item_id, created_at, request_hash FROM project_change_requests "
            "WHERE project_id = ? AND idempotency_key = ?", (project_id, key)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return ({"id": row[0], "project_id": row[1], "run_id": row[2], "status": row[3],
                 "title": row[4], "description": row[5],
                 "affected_requirement_ids": json.loads(row[6]), "requested_action": row[7],
                 "inbox_item_id": row[8], "created_at": row[9]}, row[10])

    async def insert_change_request(
        self, conn: aiosqlite.Connection, *, item: ChangeRequestView,
        idempotency_key: str, request_hash: str
    ) -> None:
        await conn.execute(
            "INSERT INTO project_change_requests (id, project_id, run_id, status, title, description, "
            "affected_requirement_ids, requested_action, inbox_item_id, idempotency_key, request_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item["id"], item["project_id"], item["run_id"], item["status"], item["title"],
             item["description"], json.dumps(item["affected_requirement_ids"], sort_keys=True),
             item["requested_action"], item["inbox_item_id"], idempotency_key, request_hash,
             item["created_at"]),
        )

    async def set_change_request_inbox(
        self, conn: aiosqlite.Connection, change_request_id: str, inbox_item_id: str
    ) -> None:
        await conn.execute(
            "UPDATE project_change_requests SET inbox_item_id = ? WHERE id = ?",
            (inbox_item_id, change_request_id),
        )


__all__ = [
    "ProjectRepository",
    "SqliteProjectRepository",
    "ProjectRecord",
    "MemberRecord",
    "project_record_to_view",
    "member_record_to_view",
    "PROJECT_EXTENSIONS_DDL",
    "init_project_extensions_schema",
]
