"""Git 协调事实和 SQLite 投影水位接口。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, cast

import aiosqlite

from maf_contracts.coordination import CoordinationEvent, CoordinationSnapshot, EventDecision
from maf_domain.errors import IdempotencyConflictError
from maf_server.core.database import Database

from .schemas import ProjectorState


class GitCoordinationRepository(Protocol):
    async def get_projector_state(self, repository_binding_id: str) -> ProjectorState | None:
        """读取当前投影水位和错误状态；不存在表示尚未初始化。"""
        ...

    async def project_snapshot(self, snapshot: CoordinationSnapshot, expected_previous_commit: str | None) -> None:
        """在一个 SQLite 事务中替换/更新任务、节点投影并推进 control commit 水位。

        expected_previous_commit 不匹配时拒绝，避免两个 projector 乱序覆盖。
        """
        ...

    async def has_processed_event(self, event_id: str) -> bool:
        """查询事件去重记录。"""
        ...

    async def record_event_decision(self, event: CoordinationEvent, decision: EventDecision) -> None:
        """保存接受/拒绝决定和原因；同 event_id 不可产生不同决定。"""
        ...


PROJECTION_SCHEMA_DDL = """\
CREATE TABLE IF NOT EXISTS git_projector_state (
    repository_binding_id     TEXT PRIMARY KEY,
    control_branch            TEXT NOT NULL,
    projected_control_commit  TEXT,
    status                    TEXT NOT NULL,
    last_error                TEXT,
    updated_at                TEXT NOT NULL,
    CHECK (status IN ('READY', 'SYNCING', 'ERROR', 'REBUILDING'))
);

CREATE TABLE IF NOT EXISTS git_coordination_tasks (
    repository_binding_id  TEXT NOT NULL,
    task_id                TEXT NOT NULL,
    status                 TEXT NOT NULL,
    owner_node_id          TEXT,
    assignment_epoch       INTEGER,
    payload_json           TEXT NOT NULL,
    PRIMARY KEY (repository_binding_id, task_id)
);

CREATE TABLE IF NOT EXISTS git_coordination_nodes (
    repository_binding_id  TEXT NOT NULL,
    node_id                TEXT NOT NULL,
    status                 TEXT NOT NULL,
    payload_json           TEXT NOT NULL,
    PRIMARY KEY (repository_binding_id, node_id)
);

CREATE TABLE IF NOT EXISTS git_coordination_events (
    repository_binding_id  TEXT NOT NULL,
    event_id               TEXT NOT NULL,
    event_type             TEXT,
    payload_json           TEXT NOT NULL,
    PRIMARY KEY (repository_binding_id, event_id)
);
"""


async def init_projection_schema(database: Database) -> None:
    """Create the rebuildable Git projection tables idempotently."""
    async with database.write_connection() as conn:
        for statement in PROJECTION_SCHEMA_DDL.split(";"):
            if statement.strip():
                await conn.execute(statement)


class SqliteGitCoordinationRepository:
    """SQLite projection whose sole authority is a Git control snapshot.

    Projection rows and the control watermark are replaced in one short
    transaction. A failed write therefore leaves both the old rows and old
    watermark intact, so replay can restart from the previous commit.
    """

    def __init__(self, database: Database, *, control_branch: str = "maf/control") -> None:
        self._database = database
        self._control_branch = control_branch

    async def initialize(self) -> None:
        await init_projection_schema(self._database)

    async def get_projector_state(
        self, repository_binding_id: str
    ) -> ProjectorState | None:
        async with self._database.read_connection() as conn:
            async with conn.execute(
                """SELECT repository_binding_id, control_branch,
                          projected_control_commit, status, last_error, updated_at
                   FROM git_projector_state WHERE repository_binding_id = ?""",
                (repository_binding_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return ProjectorState(
            repository_binding_id=str(row[0]),
            control_branch=str(row[1]),
            projected_control_commit=cast(str | None, row[2]),
            status=cast(Any, row[3]),
            last_error=cast(str | None, row[4]),
            updated_at=str(row[5]),
        )

    async def project_snapshot(
        self,
        snapshot: CoordinationSnapshot,
        expected_previous_commit: str | None,
    ) -> None:
        binding_id = str(snapshot["project_id"])
        commit = str(snapshot["control_commit"])
        async with self._database.write_connection() as conn:
            await self._replace_snapshot(
                conn,
                binding_id=binding_id,
                snapshot=snapshot,
                commit=commit,
                expected_previous_commit=expected_previous_commit,
                rebuilding=False,
            )

    async def rebuild_projection(self, snapshot: CoordinationSnapshot) -> str:
        """Clear and rebuild one project's projection in a single transaction."""
        binding_id = str(snapshot["project_id"])
        commit = str(snapshot["control_commit"])
        async with self._database.write_connection() as conn:
            await self._replace_snapshot(
                conn,
                binding_id=binding_id,
                snapshot=snapshot,
                commit=commit,
                expected_previous_commit=None,
                rebuilding=True,
            )
        return commit

    async def _replace_snapshot(
        self,
        conn: aiosqlite.Connection,
        *,
        binding_id: str,
        snapshot: CoordinationSnapshot,
        commit: str,
        expected_previous_commit: str | None,
        rebuilding: bool,
    ) -> None:
        async with conn.execute(
            """SELECT projected_control_commit FROM git_projector_state
               WHERE repository_binding_id = ?""",
            (binding_id,),
        ) as cursor:
            state = await cursor.fetchone()
        actual_previous = None if state is None else cast(str | None, state[0])
        if not rebuilding and actual_previous != expected_previous_commit:
            raise IdempotencyConflictError(
                "projector control watermark changed",
                context={
                    "repository_binding_id": binding_id,
                    "expected_previous_commit": expected_previous_commit,
                    "actual_previous_commit": actual_previous,
                },
            )

        for table in (
            "git_coordination_tasks",
            "git_coordination_nodes",
            "git_coordination_events",
        ):
            await conn.execute(
                f"DELETE FROM {table} WHERE repository_binding_id = ?",  # noqa: S608
                (binding_id,),
            )

        tasks = sorted(snapshot.get("tasks", []), key=lambda item: str(item["task_id"]))
        for task in tasks:
            assignment = task.get("assignment") or {}
            await conn.execute(
                """INSERT INTO git_coordination_tasks
                   (repository_binding_id, task_id, status, owner_node_id,
                    assignment_epoch, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    binding_id,
                    str(task["task_id"]),
                    str(task["status"]),
                    assignment.get("node_id"),
                    assignment.get("assignment_epoch"),
                    _canonical_json(task),
                ),
            )

        nodes = sorted(snapshot.get("nodes", []), key=lambda item: str(item["node_id"]))
        for node in nodes:
            await conn.execute(
                """INSERT INTO git_coordination_nodes
                   (repository_binding_id, node_id, status, payload_json)
                   VALUES (?, ?, ?, ?)""",
                (binding_id, str(node["node_id"]), str(node["status"]), _canonical_json(node)),
            )

        raw_snapshot = cast(dict[str, Any], snapshot)
        events = list(raw_snapshot.get("events", []))
        if not events:
            events = [
                {"event_id": path, "event_type": None, "path": path}
                for path in raw_snapshot.get("events_paths", [])
            ]
        for event in sorted(events, key=lambda item: str(item["event_id"])):
            await conn.execute(
                """INSERT INTO git_coordination_events
                   (repository_binding_id, event_id, event_type, payload_json)
                   VALUES (?, ?, ?, ?)""",
                (
                    binding_id,
                    str(event["event_id"]),
                    event.get("event_type"),
                    _canonical_json(event),
                ),
            )

        # The watermark is deliberately the final statement in this transaction.
        now = _now_iso()
        await conn.execute(
            """INSERT INTO git_projector_state
               (repository_binding_id, control_branch, projected_control_commit,
                status, last_error, updated_at)
               VALUES (?, ?, ?, 'READY', NULL, ?)
               ON CONFLICT(repository_binding_id) DO UPDATE SET
                 control_branch = excluded.control_branch,
                 projected_control_commit = excluded.projected_control_commit,
                 status = 'READY', last_error = NULL, updated_at = excluded.updated_at""",
            (binding_id, self._control_branch, commit, now),
        )

    async def list_projected_tasks(self, repository_binding_id: str) -> list[dict[str, Any]]:
        """Read projected task payloads in deterministic order (query/test helper)."""
        async with self._database.read_connection() as conn:
            async with conn.execute(
                """SELECT payload_json FROM git_coordination_tasks
                   WHERE repository_binding_id = ? ORDER BY task_id""",
                (repository_binding_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [cast(dict[str, Any], json.loads(str(row[0]))) for row in rows]


# --------------------------------------------------------------------------- #
# TASK-021：事件幂等与判定记录
#
# ``event_decisions`` 表以 ``(event_id, consumer_id)`` 为主键记录每个消费者对
# 每个事件的处理判定，支持：
#   - 幂等去重：同一消费者重复处理同一事件时跳过（返回首次决定）；
#   - 内容冲突检测：同 event_id 不同内容抛 ``IdempotencyConflictError``；
#   - 失败重试：decision 为 ``failed`` 的记录可被覆盖更新。
#
# 该表只记录事件处理判定，不修改事件本身（事件内容是 Git coordination 事实源）。
# --------------------------------------------------------------------------- #


#: ``event_decisions.decision`` 列的判定值。
#:
#: - ``applied``：事件已成功应用（PROCESSED 类）；
#: - ``skipped_duplicate``：因已处理而跳过（SKIPPED 类）；
#: - ``skipped_invalid``：因事件内容非法而跳过（SKIPPED 类）；
#: - ``failed``：处理失败，可重试（FAILED 类）。
EVENT_DECISION_APPLIED = "applied"
EVENT_DECISION_SKIPPED_DUPLICATE = "skipped_duplicate"
EVENT_DECISION_SKIPPED_INVALID = "skipped_invalid"
EVENT_DECISION_FAILED = "failed"

#: 可重试的判定集合（FAILED 类）。这类记录可被后续 ``record_decision`` 覆盖，
#: 以支持失败后重试成功时更新判定。
_RETRYABLE_DECISIONS = frozenset({EVENT_DECISION_FAILED})

#: 关闭内容冲突检测的哨兵值。当调用方不关心内容冲突时传入，``record_decision``
#: 不会因内容哈希不同而抛 ``IdempotencyConflictError``。
NO_CONTENT_HASH = ""


EVENT_DECISIONS_DDL = """\
CREATE TABLE IF NOT EXISTS event_decisions (
    event_id      TEXT    NOT NULL,
    consumer_id   TEXT    NOT NULL,
    decision      TEXT    NOT NULL,
    result        TEXT,
    error         TEXT,
    content_hash  TEXT    NOT NULL,
    processed_at  TEXT    NOT NULL,
    version_no    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (event_id, consumer_id)
);
"""


async def init_event_decisions_schema(database: Database) -> None:
    """幂等创建 ``event_decisions`` 表。

    正式 schema 由迁移管理（TASK-007）；本函数供测试和早期开发在迁移未应用前
    使用，``CREATE TABLE IF NOT EXISTS`` 幂等。
    """
    async with database.write_connection() as conn:
        await conn.execute(EVENT_DECISIONS_DDL)


def compute_event_content_hash(event: CoordinationEvent) -> str:
    """计算 ``CoordinationEvent`` 内容的 SHA-256 哈希。

    用于 ``record_event_decision`` 的幂等冲突检测：同一 ``event_id`` 但内容不同
    的事件哈希不同，判定为冲突（《TASK-021》验收标准：同 event_id 不同内容
    被判冲突）。

    哈希基于事件全字段（``sort_keys=True`` 保证字典序稳定），不含事件在 Git 中的
    存储路径或时间戳漂移，只反映事件内容本身。
    """
    payload = json.dumps(event, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EventDecisionRecord:
    """``event_decisions`` 表一行的只读视图。"""

    event_id: str
    consumer_id: str
    decision: str
    result: str | None
    error: str | None
    content_hash: str
    processed_at: datetime
    version_no: int

    @property
    def is_processed(self) -> bool:
        """是否已成功处理（PROCESSED/SKIPPED 类，非 FAILED）。"""
        return self.decision not in _RETRYABLE_DECISIONS


class EventDecisionRepository:
    """事件处理判定仓库，绑定到 ``UnitOfWork`` 事务连接。

    与 ``SqliteEventPublisher`` 一样，本类不在内部管理事务或连接生命周期——
    调用方在 ``async with SqliteUnitOfWork(database) as uow:`` 块内构造
    ``EventDecisionRepository(uow.connection)``，所有读写随 UoW ``commit``/
    ``rollback`` 原子提交或回滚。

    幂等策略：

    - ``has_processed``：返回 ``True`` 当且仅当存在判定且非 ``failed``
      （``failed`` 表示处理失败可重试，故视为未完成）；
    - ``record_decision``：
        - 无记录 → INSERT；
        - 有记录且 ``content_hash`` 不同 → 抛 ``IdempotencyConflictError``；
        - 有记录且 ``content_hash`` 相同且原判定为 ``failed`` → UPDATE（允许重试）；
        - 有记录且 ``content_hash`` 相同且原判定非 ``failed`` → 幂等无操作
          （返回首次决定，符合《TASK-021》验收：同 event_id 相同内容返回首次决定）。

    事务边界约束（《GitHub 分布式协作协议》§6.6、§10）：

    - 写事务必须短，只做 SQL，不在事务中调用模型、Docker、Git 或网络；
    - 本类不修改事件内容，只记录判定。
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def has_processed(self, event_id: str, consumer_id: str) -> bool:
        """检查 ``(event_id, consumer_id)`` 是否已成功处理。

        ``failed`` 判定视为未完成（可重试），返回 ``False``。
        """
        async with self._conn.execute(
            "SELECT decision FROM event_decisions WHERE event_id = ? AND consumer_id = ?",
            (event_id, consumer_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False
        decision: str = row[0]
        return decision not in _RETRYABLE_DECISIONS

    async def record_decision(
        self,
        event_id: str,
        consumer_id: str,
        decision: str,
        result: str | None,
        error: str | None,
        content_hash: str = NO_CONTENT_HASH,
    ) -> None:
        """记录事件处理判定；幂等。

        :param event_id: 事件全局唯一 ID。
        :param consumer_id: 消费者标识（如 projector 名）。
        :param decision: 判定值，取 ``EVENT_DECISION_*`` 常量。
        :param result: 处理结果摘要（如影响的 task_id、新状态），可空。
        :param error: 失败时的错误信息，``None`` 表示无错误。
        :param content_hash: 事件内容哈希，用于检测同 event_id 不同内容冲突；
            ``NO_CONTENT_HASH`` 关闭冲突检测。
        :raises IdempotencyConflictError: 同 ``(event_id, consumer_id)`` 已存在
            但 ``content_hash`` 不同。
        """
        existing = await self._fetch(event_id, consumer_id)
        now = _now_iso()
        if existing is None:
            await self._conn.execute(
                """INSERT INTO event_decisions
                   (event_id, consumer_id, decision, result, error,
                    content_hash, processed_at, version_no)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                (event_id, consumer_id, decision, result, error, content_hash, now),
            )
            return

        # 已有记录：检查内容冲突
        if content_hash != NO_CONTENT_HASH and existing.content_hash != NO_CONTENT_HASH:
            if existing.content_hash != content_hash:
                raise IdempotencyConflictError(
                    f"事件 {event_id!r} 已被 consumer {consumer_id!r} 以不同内容处理过",
                    context={
                        "event_id": event_id,
                        "consumer_id": consumer_id,
                        "existing_hash": existing.content_hash,
                        "incoming_hash": content_hash,
                    },
                )

        # 内容一致：仅当原判定可重试（failed）时覆盖更新，否则幂等无操作
        if existing.decision in _RETRYABLE_DECISIONS:
            await self._conn.execute(
                """UPDATE event_decisions
                   SET decision = ?, result = ?, error = ?,
                       processed_at = ?, version_no = version_no + 1
                   WHERE event_id = ? AND consumer_id = ?""",
                (decision, result, error, now, event_id, consumer_id),
            )
        # else: PROCESSED/SKIPPED → 幂等，保留首次决定

    async def get_decision(
        self, event_id: str, consumer_id: str
    ) -> EventDecisionRecord | None:
        """读取判定记录；不存在返回 ``None``。"""
        row = await self._fetch(event_id, consumer_id)
        return row

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    async def _fetch(
        self, event_id: str, consumer_id: str
    ) -> EventDecisionRecord | None:
        async with self._conn.execute(
            """SELECT event_id, consumer_id, decision, result, error,
                      content_hash, processed_at, version_no
               FROM event_decisions
               WHERE event_id = ? AND consumer_id = ?""",
            (event_id, consumer_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_record(row)


def _row_to_record(row: aiosqlite.Row | tuple) -> EventDecisionRecord:
    """把 SELECT 行转为 ``EventDecisionRecord``。"""
    (
        event_id,
        consumer_id,
        decision,
        result,
        error,
        content_hash,
        processed_at,
        version_no,
    ) = tuple(row)
    return EventDecisionRecord(
        event_id=event_id,
        consumer_id=consumer_id,
        decision=decision,
        result=result,
        error=error,
        content_hash=content_hash,
        processed_at=datetime.fromisoformat(processed_at),
        version_no=int(version_no),
    )


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "GitCoordinationRepository",
    "EventDecisionRepository",
    "EventDecisionRecord",
    "EVENT_DECISIONS_DDL",
    "EVENT_DECISION_APPLIED",
    "EVENT_DECISION_SKIPPED_DUPLICATE",
    "EVENT_DECISION_SKIPPED_INVALID",
    "EVENT_DECISION_FAILED",
    "NO_CONTENT_HASH",
    "compute_event_content_hash",
    "init_event_decisions_schema",
    "PROJECTION_SCHEMA_DDL",
    "SqliteGitCoordinationRepository",
    "init_projection_schema",
]
