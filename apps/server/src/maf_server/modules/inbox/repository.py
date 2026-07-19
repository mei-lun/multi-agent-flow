"""Inbox Item 持久化接口（TASK-082）。

定义 ``inbox_items`` 表 DDL 与 SQLite 仓储实现：

- ``INBOX_ITEMS_DDL``：``inbox_items`` 表 DDL（id、project_id、title、
  description、item_type、artifact_id、review_id、assigned_to、priority、
  status、decision、decision_comment、decided_by、decided_at、created_at、
  created_by、metadata TEXT JSON、version_no）。
- ``init_inbox_schema``：幂等建表函数（供测试与首次启动使用）。
- ``SqliteInboxRepository``：``inbox_items`` 表 CRUD，提供 insert/get/
  list_for_actor/update_decision/assign/expire 方法，接受
  ``aiosqlite.Connection``（由 ``SqliteUnitOfWork`` 提供），不自开事务。
- ``InboxItemRecord`` dataclass：行映射，供 service 层使用。

事务边界：repository 方法接受 ``aiosqlite.Connection``（由
``SqliteUnitOfWork`` 提供），不自开事务、不调用网络、不写日志；service 层
负责 ``BEGIN IMMEDIATE``/``COMMIT``。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import aiosqlite

from .schemas import (
    InboxDecision,
    InboxItemStatus,
    InboxItemType,
    InboxItemView,
    InboxPriority,
)

# --------------------------------------------------------------------------- #
# 表结构 DDL（供测试与首次启动建表使用；正式部署由 migrations 负责）
# --------------------------------------------------------------------------- #

INBOX_ITEMS_DDL: str = """
CREATE TABLE IF NOT EXISTS inbox_items (
    id                TEXT    PRIMARY KEY,
    project_id        TEXT    NOT NULL,
    title             TEXT    NOT NULL,
    description       TEXT    NOT NULL DEFAULT '',
    item_type         TEXT    NOT NULL,
    artifact_id       TEXT,
    review_id         TEXT,
    assigned_to       TEXT,
    priority          TEXT    NOT NULL DEFAULT 'NORMAL',
    status            TEXT    NOT NULL DEFAULT 'PENDING',
    decision          TEXT,
    decision_comment  TEXT,
    decided_by        TEXT,
    decided_at        TEXT,
    created_at        TEXT    NOT NULL,
    created_by        TEXT    NOT NULL,
    metadata          TEXT    NOT NULL DEFAULT '{}' CHECK(json_valid(metadata)),
    version_no        INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_inbox_items_assigned
    ON inbox_items(assigned_to);
CREATE INDEX IF NOT EXISTS idx_inbox_items_status
    ON inbox_items(status);
CREATE INDEX IF NOT EXISTS idx_inbox_items_project
    ON inbox_items(project_id);
CREATE INDEX IF NOT EXISTS idx_inbox_items_review
    ON inbox_items(review_id);
"""


async def init_inbox_schema(conn: aiosqlite.Connection) -> None:
    """在给定连接上创建 ``inbox_items`` 表与索引（``CREATE TABLE IF NOT
    EXISTS``，幂等）。

    正式部署由 ``migrations/`` 顺序迁移负责；本函数供测试与开发期首次启动
    使用。

    实现说明：使用逐条 ``execute`` 而非 ``executescript``，因为
    ``executescript`` 会隐式 COMMIT 当前事务，与 ``Database.write_connection``
    的 ``BEGIN IMMEDIATE``/``COMMIT`` 边界冲突。
    """
    for raw in INBOX_ITEMS_DDL.split(";"):
        stmt = raw.strip()
        if stmt:
            await conn.execute(stmt)


def new_inbox_item_id() -> str:
    """生成新 Inbox Item ID（UUID v4 字符串）。"""
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- #
# inbox_items 行映射
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class InboxItemRecord:
    """``inbox_items`` 表行映射，供 service 层内部使用。"""

    id: str
    project_id: str
    title: str
    description: str
    item_type: InboxItemType
    artifact_id: str | None
    review_id: str | None
    assigned_to: str | None
    priority: InboxPriority
    status: InboxItemStatus
    decision: InboxDecision | None
    decision_comment: str | None
    decided_by: str | None
    decided_at: str | None
    created_at: str
    created_by: str
    version_no: int
    metadata: dict[str, Any]


_INBOX_COLUMNS: str = (
    "id, project_id, title, description, item_type, artifact_id, "
    "review_id, assigned_to, priority, status, decision, "
    "decision_comment, decided_by, decided_at, created_at, "
    "created_by, version_no, metadata"
)

#: 合法状态白名单（防御：DB 层无 CHECK，service 层保证取值合法）。
_VALID_STATUSES = frozenset({"PENDING", "DECIDED", "EXPIRED"})
_VALID_ITEM_TYPES = frozenset(
    {"REVIEW_REQUEST", "CHANGE_REQUEST", "APPROVAL_REQUEST"}
)
_VALID_DECISIONS = frozenset({"APPROVE", "REJECT", "REQUEST_CHANGES"})
_VALID_PRIORITIES = frozenset({"LOW", "NORMAL", "HIGH", "URGENT"})


def _row_to_record(row: aiosqlite.Row | tuple) -> InboxItemRecord:
    """把 ``inbox_items`` 表行映射为 ``InboxItemRecord``。"""
    if isinstance(row, aiosqlite.Row):
        row = tuple(row)
    raw_meta = str(row[17])
    try:
        meta = json.loads(raw_meta)
    except json.JSONDecodeError:  # 防御：CHECK(json_valid) 已保证
        meta = {}
    if not isinstance(meta, dict):
        meta = {}

    status = str(row[9]) if row[9] is not None else "PENDING"
    if status not in _VALID_STATUSES:
        status = "PENDING"
    item_type = str(row[4]) if row[4] is not None else "REVIEW_REQUEST"
    if item_type not in _VALID_ITEM_TYPES:
        item_type = "REVIEW_REQUEST"
    priority = str(row[8]) if row[8] is not None else "NORMAL"
    if priority not in _VALID_PRIORITIES:
        priority = "NORMAL"
    decision = row[10]
    if decision is not None and str(decision) not in _VALID_DECISIONS:
        decision = None

    return InboxItemRecord(
        id=str(row[0]),
        project_id=str(row[1]),
        title=str(row[2]),
        description=str(row[3]) if row[3] is not None else "",
        item_type=item_type,  # type: ignore[arg-type]
        artifact_id=row[5],  # str | None
        review_id=row[6],  # str | None
        assigned_to=row[7],  # str | None
        priority=priority,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        decision=decision,  # type: ignore[arg-type]
        decision_comment=row[11],  # str | None
        decided_by=row[12],  # str | None
        decided_at=row[13],  # str | None
        created_at=str(row[14]),
        created_by=str(row[15]),
        version_no=int(row[16]),
        metadata=meta,
    )


def record_to_view(rec: InboxItemRecord) -> InboxItemView:
    """把 ``InboxItemRecord`` 映射为对外 ``InboxItemView``。"""
    return InboxItemView(
        id=rec.id,
        project_id=rec.project_id,
        title=rec.title,
        description=rec.description,
        item_type=rec.item_type,
        artifact_id=rec.artifact_id,
        review_id=rec.review_id,
        assigned_to=rec.assigned_to,
        priority=rec.priority,
        status=rec.status,
        decision=rec.decision,
        decision_comment=rec.decision_comment,
        decided_by=rec.decided_by,
        decided_at=rec.decided_at,
        created_at=rec.created_at,
        created_by=rec.created_by,
        version_no=rec.version_no,
        metadata=rec.metadata,
    )


# --------------------------------------------------------------------------- #
# SqliteInboxRepository
# --------------------------------------------------------------------------- #


class SqliteInboxRepository:
    """``inbox_items`` 表的 SQLite 仓储实现。

    所有方法接受 ``aiosqlite.Connection``（由 ``SqliteUnitOfWork`` 提供），
    不自开事务、不调用网络、不写日志。

    谁调用它：
        ``InboxServiceImpl`` 在 ``SqliteUnitOfWork`` 事务内调用本类方法
        读写 inbox 待办项。

    安全约束：
        - ``metadata`` 以 TEXT 存储，``CHECK(json_valid)`` 在 DB 层保证合法 JSON；
        - ``status``/``item_type``/``decision``/``priority`` 取值由 service 层
          校验，本类只负责持久化；
        - 乐观锁：``update_decision``/``assign``/``expire`` 通过
          ``version_no`` 防止并发覆盖。
    """

    async def insert_item(
        self,
        conn: aiosqlite.Connection,
        *,
        item_id: str,
        project_id: str,
        title: str,
        description: str,
        item_type: str,
        artifact_id: str | None,
        review_id: str | None,
        assigned_to: str | None,
        priority: str,
        status: str,
        created_at: str,
        created_by: str,
        metadata: dict[str, Any],
    ) -> None:
        """插入一行 inbox 待办项。

        调用方应保证 ``item_type``/``priority``/``status`` 取值合法。
        """
        await conn.execute(
            "INSERT INTO inbox_items "
            "(id, project_id, title, description, item_type, artifact_id, "
            "review_id, assigned_to, priority, status, decision, "
            "decision_comment, decided_by, decided_at, created_at, "
            "created_by, metadata, version_no) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, "
            "?, ?, ?, 1)",
            (
                item_id,
                project_id,
                title,
                description,
                item_type,
                artifact_id,
                review_id,
                assigned_to,
                priority,
                status,
                created_at,
                created_by,
                json.dumps(metadata, ensure_ascii=False),
            ),
        )

    async def get_item(
        self,
        conn: aiosqlite.Connection,
        item_id: str,
    ) -> InboxItemRecord | None:
        """按 id 查询待办项；不存在返回 None。"""
        sql = (
            f"SELECT {_INBOX_COLUMNS} FROM inbox_items "
            "WHERE id = ? LIMIT 1"
        )
        async with conn.execute(sql, (item_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_record(row) if row is not None else None

    async def list_for_actor(
        self,
        conn: aiosqlite.Connection,
        actor_id: str,
        *,
        status: str | None = None,
        project_id: str | None = None,
        limit: int = 200,
    ) -> list[InboxItemRecord]:
        """列出当前用户可见的待办项。

        可见性规则（对应任务目标 2）：
            - ``assigned_to == actor_id``（显式分配给该用户）；
            - 或 ``assigned_to IS NULL``（所有 APPROVER 可见）。

        按 ``created_at`` 降序（最新在前），默认上限 200，最大 500。
        """
        effective_limit = max(1, min(limit, 500))
        where_parts = [
            "(assigned_to = ? OR assigned_to IS NULL)",
        ]
        params: list[Any] = [actor_id]
        if status is not None:
            where_parts.append("status = ?")
            params.append(status)
        if project_id is not None:
            where_parts.append("project_id = ?")
            params.append(project_id)

        sql = (
            f"SELECT {_INBOX_COLUMNS} FROM inbox_items "
            "WHERE " + " AND ".join(where_parts) +
            " ORDER BY created_at DESC, id ASC LIMIT ?"
        )
        params.append(effective_limit)
        async with conn.execute(sql, tuple(params)) as cur:
            rows = list(await cur.fetchall())
        return [_row_to_record(r) for r in rows]

    async def update_decision(
        self,
        conn: aiosqlite.Connection,
        item_id: str,
        *,
        decision: str,
        decision_comment: str,
        decided_by: str,
        decided_at: str,
        expected_version: int,
    ) -> int:
        """更新待办项为 DECIDED（乐观锁）。

        :returns: 新版本号（>0）表示更新成功；0 表示版本冲突或记录不存在，
            或记录已不在 PENDING 状态。
        """
        cur = await conn.execute(
            "UPDATE inbox_items "
            "SET status = 'DECIDED', decision = ?, decision_comment = ?, "
            "decided_by = ?, decided_at = ?, "
            "version_no = version_no + 1 "
            "WHERE id = ? AND version_no = ? AND status = 'PENDING'",
            (
                decision,
                decision_comment,
                decided_by,
                decided_at,
                item_id,
                expected_version,
            ),
        )
        new_version = cur.rowcount if cur.rowcount > 0 else 0
        await cur.close()
        return new_version

    async def update_assigned_to(
        self,
        conn: aiosqlite.Connection,
        item_id: str,
        *,
        assigned_to: str | None,
        expected_version: int,
    ) -> int:
        """更新待办项的 assignee（乐观锁）。

        :returns: 新版本号（>0）表示更新成功；0 表示版本冲突或记录不存在。
        """
        cur = await conn.execute(
            "UPDATE inbox_items "
            "SET assigned_to = ?, version_no = version_no + 1 "
            "WHERE id = ? AND version_no = ?",
            (assigned_to, item_id, expected_version),
        )
        new_version = cur.rowcount if cur.rowcount > 0 else 0
        await cur.close()
        return new_version

    async def update_status_expired(
        self,
        conn: aiosqlite.Connection,
        item_id: str,
        *,
        expected_version: int,
    ) -> int:
        """更新待办项为 EXPIRED（乐观锁）。

        :returns: 新版本号（>0）表示更新成功；0 表示版本冲突或记录不存在，
            或记录已不在 PENDING 状态。
        """
        cur = await conn.execute(
            "UPDATE inbox_items "
            "SET status = 'EXPIRED', version_no = version_no + 1 "
            "WHERE id = ? AND version_no = ? AND status = 'PENDING'",
            (item_id, expected_version),
        )
        new_version = cur.rowcount if cur.rowcount > 0 else 0
        await cur.close()
        return new_version


__all__ = [
    "INBOX_ITEMS_DDL",
    "InboxItemRecord",
    "SqliteInboxRepository",
    "init_inbox_schema",
    "new_inbox_item_id",
    "record_to_view",
]
