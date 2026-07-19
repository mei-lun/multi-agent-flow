"""进程内发布、持久事件与 Outbox 接口。

根据《多 Agent 协同工具系统设计文档》§6.5、§10.7、§10.8 与《GitHub 分布式
协作协议》：

- ``EventPublisher`` 在 ``UnitOfWork`` 事务内把领域事件写入 ``outbox_events`` 表，
  与业务修改同事务提交/回滚（协议 §6.5、《接口设计与实现规范》§6 步骤 9）。
- ``OutboxRepository`` 提供 Outbox 查询、发布标记、重试与消费幂等原语：
    - ``list_unpublished``：按 ``occurred_at`` 升序取未发布事件；
    - ``mark_published`` / ``mark_failed``：记录发布结果，失败递增重试计数；
    - ``find_by_run`` / ``find_by_project``：按 run/project 查询事件流；
    - ``mark_consumed`` / ``has_consumed`` / ``consume``：以
      ``(consumer_name, event_id)`` 为幂等键，保证重复消费不产生重复投影副作用
      （设计文档 §10.8）。
- Outbox 是本地投影/通知机制，不是跨节点事实源；Git coordination 事件才是跨节点
  事实源，本模块不与 Git coordination 事件语义混淆。
- 写事务必须短：``append`` 只做 SQL INSERT，不在事务中调用模型、Docker、Git 或网络。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Protocol, cast

import aiosqlite

from maf_contracts.events import ActorRef, DomainEvent, EventEnvelope
from maf_server.core.database import Database

# --------------------------------------------------------------------------- #
# Schema DDL
#
# 正式 schema 由迁移管理（TASK-007）；``init_outbox_schema`` 供测试和早期开发在
# 迁移未应用前使用，``CREATE TABLE IF NOT EXISTS`` 幂等。物理类型映射遵循设计
# 文档 §7：``uuid→TEXT``、``json→TEXT + json_valid CHECK``、``datetime→TEXT(RFC3339)``、
# ``bigint→INTEGER``。
# --------------------------------------------------------------------------- #

OUTBOX_EVENTS_DDL = """\
CREATE TABLE IF NOT EXISTS outbox_events (
    id               TEXT    PRIMARY KEY,
    event_type       TEXT    NOT NULL,
    schema_version   INTEGER NOT NULL,
    aggregate_type   TEXT    NOT NULL,
    aggregate_id     TEXT    NOT NULL,
    organization_id  TEXT    NOT NULL,
    project_id       TEXT,
    run_id           TEXT,
    occurred_at      TEXT    NOT NULL,
    actor_type       TEXT    NOT NULL,
    actor_id         TEXT    NOT NULL,
    trace_id         TEXT    NOT NULL,
    payload          TEXT    NOT NULL CHECK(json_valid(payload)),
    published_at     TEXT,
    publish_attempts INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT
);
"""

OUTBOX_INDEXES_DDL = """\
CREATE INDEX IF NOT EXISTS idx_outbox_published_occurred
    ON outbox_events(published_at, occurred_at);
CREATE INDEX IF NOT EXISTS idx_outbox_run_occurred
    ON outbox_events(run_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_outbox_project_occurred
    ON outbox_events(project_id, occurred_at);
"""

OUTBOX_CONSUMPTIONS_DDL = """\
CREATE TABLE IF NOT EXISTS outbox_consumptions (
    event_id        TEXT    NOT NULL,
    consumer_name   TEXT    NOT NULL,
    consumed_at     TEXT    NOT NULL,
    PRIMARY KEY (event_id, consumer_name)
);
"""


async def init_outbox_schema(database: Database) -> None:
    """幂等创建 ``outbox_events`` 与 ``outbox_consumptions`` 表及索引。

    正式 schema 由迁移管理；本函数供测试和早期开发在迁移未应用前使用。

    实现说明：使用逐条 ``execute`` 而非 ``executescript``，因为
    ``executescript`` 会隐式 COMMIT 当前事务，与 ``write_connection`` 的
    ``BEGIN IMMEDIATE``/``COMMIT`` 边界冲突。
    """
    statements = _split_sql(OUTBOX_EVENTS_DDL + OUTBOX_INDEXES_DDL + OUTBOX_CONSUMPTIONS_DDL)
    async with database.write_connection() as conn:
        for stmt in statements:
            await conn.execute(stmt)


def _split_sql(sql: str) -> list[str]:
    """把多语句 DDL 按分号拆分为单语句列表。

    DDL 不含字符串字面量中的分号，简单按 ``;`` 拆分即可；空语句被过滤。
    """
    return [s.strip() for s in sql.split(";") if s.strip()]


#: 事件处理回调类型：接收 ``DomainEvent``，可能抛异常表示处理失败。
EventHandler = Callable[[DomainEvent], Awaitable[None]]


# --------------------------------------------------------------------------- #
# EventPublisher 协议与 SQLite 实现
# --------------------------------------------------------------------------- #


class EventPublisher(Protocol):
    """在 ``UnitOfWork`` 事务内把领域事件写入 Outbox 的发布者协议。

    谁调用它：
        应用服务层在 ``async with unit_of_work:`` 块内调用 ``append``，与业务
        Repository 写入共用同一连接和事务（《接口设计与实现规范》§6 步骤 6-9）。

    事务边界：
        - ``append`` 必须在 UoW 事务内调用；它不开启自己的事务，只执行 INSERT，
          随 UoW ``commit``/``rollback`` 一起原子提交或回滚；
        - ``event_id`` 必须唯一，重复插入会触发 ``IntegrityError``（由调用方处理）。
    """

    async def append(self, event: DomainEvent) -> None:
        """把领域事件追加到当前事务的 ``outbox_events`` 表。

        :param event: 领域事件；``event_id`` 必须全局唯一。
        :raises sqlite3.IntegrityError: ``event_id`` 已存在。
        """
        ...


class SqliteEventPublisher:
    """``EventPublisher`` 的 SQLite 实现，绑定到 ``UnitOfWork`` 事务连接。

    使用方式::

        async with SqliteUnitOfWork(database) as uow:
            await uow.connection.execute("INSERT INTO tasks ...")
            publisher = SqliteEventPublisher(uow.connection)
            await publisher.append(TaskCreated(...))
            await uow.commit()

    ``append`` 只做 INSERT，不在事务中调用模型、Docker、Git 或网络。事件与业务
    修改由 ``uow.commit()`` 原子提交；若 UoW 回滚，Outbox 事件也一并回滚。
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def append(self, event: DomainEvent) -> None:
        """把领域事件 INSERT 到 ``outbox_events``。

        输入来源与可信度：
            - ``event``：由应用服务层构造，字段经 pydantic 校验；
            - ``event_id``：UUID 字符串，作为主键去重；
            - ``payload``：``json.dumps`` 序列化为 TEXT，``json_valid`` CHECK 保证。

        业务错误：
            - ``sqlite3.IntegrityError``：``event_id`` 已存在，调用方应视为编程错误
              或上游重复提交。
        """
        await self._conn.execute(
            """INSERT INTO outbox_events
               (id, event_type, schema_version, aggregate_type, aggregate_id,
                organization_id, project_id, run_id, occurred_at,
                actor_type, actor_id, trace_id, payload,
                published_at, publish_attempts, last_error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL)""",
            (
                event.event_id,
                event.event_type,
                event.schema_version,
                event.aggregate_type,
                event.aggregate_id,
                event.organization_id,
                event.project_id,
                event.run_id,
                event.occurred_at.isoformat(),
                event.actor.actor_type,
                event.actor.actor_id,
                event.trace_id,
                json.dumps(event.payload, ensure_ascii=False, default=str),
            ),
        )


# --------------------------------------------------------------------------- #
# OutboxRepository 协议与 SQLite 实现
# --------------------------------------------------------------------------- #


class OutboxRepository(Protocol):
    """Outbox 查询、发布标记、重试与消费幂等原语协议。

    谁调用它：
        - Outbox 发布协程（``publish_pending``）读取未发布事件并分发；
        - SSE 端点（``find_by_run``/``subscribe_run``）回放运行事件流；
        - 投影消费者（``consume``）以 ``(consumer_name, event_id)`` 幂等键消费事件。

    事务边界：
        - 查询使用只读短连接；
        - ``mark_published``/``mark_failed``/``mark_consumed`` 使用独立短写事务，
          不与原始业务事务耦合（事件已落库后，发布是异步动作）。
    """

    async def list_unpublished(self, limit: int = 100) -> list[EventEnvelope]:
        """按 ``occurred_at`` 升序取未发布事件（``published_at IS NULL``）。"""
        ...

    async def mark_published(self, event_id: str) -> None:
        """标记事件已发布（设置 ``published_at``，清空 ``last_error``）。"""
        ...

    async def mark_failed(self, event_id: str, error: str) -> None:
        """记录发布失败：递增 ``publish_attempts``，写入 ``last_error``。"""
        ...

    async def find_by_run(
        self,
        run_id: str,
        after_event_id: str | None = None,
        limit: int = 100,
    ) -> list[DomainEvent]:
        """按 ``run_id`` 查询事件流，按插入顺序升序。``after_event_id`` 用于断线续传。"""
        ...

    async def find_by_project(
        self,
        project_id: str,
        after_event_id: str | None = None,
        limit: int = 100,
    ) -> list[DomainEvent]:
        """按 ``project_id`` 查询事件流，按插入顺序升序。"""
        ...

    async def subscribe_run(
        self,
        run_id: str,
        after_event_id: str | None = None,
        limit: int = 100,
    ) -> AsyncIterator[DomainEvent]:
        """先回放持久事件再订阅新事件，供 SSE 使用。"""
        ...

    async def get_event(self, event_id: str) -> DomainEvent | None:
        """按 ``event_id`` 取单条事件；不存在返回 ``None``。"""
        ...

    async def mark_consumed(self, event_id: str, consumer_name: str) -> bool:
        """幂等标记事件被某消费者消费。

        :returns: ``True`` 表示首次消费（INSERT 成功）；``False`` 表示已消费过
            （主键冲突，重复消费）。
        """
        ...

    async def has_consumed(self, event_id: str, consumer_name: str) -> bool:
        """检查 ``(event_id, consumer_name)`` 是否已记录消费。"""
        ...

    async def consume(
        self,
        event_id: str,
        consumer_name: str,
        handler: EventHandler,
    ) -> bool:
        """幂等消费事件：先查消费记录，未消费则执行 handler，成功后标记消费。

        - handler 抛异常时不标记消费，调用方可重试；
        - 已消费则跳过 handler，直接返回 ``False``（无副作用）；
        - 即使 handler 内部副作用非幂等，``mark_consumed`` 的主键约束保证只有一个
          调用者真正执行 handler（单写者模型下无并发竞争）。
        """
        ...

    async def publish_pending(
        self,
        handler: EventHandler | None = None,
        batch_size: int = 100,
    ) -> int:
        """租用未发布事件，调用 handler 处理，成功标记 published，失败标记 failed。"""
        ...


class SqliteOutboxRepository:
    """``OutboxRepository`` 的 SQLite 实现，使用 ``Database`` 独立短连接。

    与 ``SqliteEventPublisher`` 不同，本类不绑定 UoW 事务连接——它管理的是事件
    落库之后的异步发布与消费流程。每个方法使用独立短读/写连接。
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    async def list_unpublished(self, limit: int = 100) -> list[EventEnvelope]:
        """按 ``occurred_at`` 升序取未发布事件。"""
        async with self._database.read_connection() as conn:
            async with conn.execute(
                """SELECT id, event_type, schema_version, aggregate_type, aggregate_id,
                          organization_id, project_id, run_id, occurred_at,
                          actor_type, actor_id, trace_id, payload,
                          published_at, publish_attempts, last_error
                   FROM outbox_events
                   WHERE published_at IS NULL
                   ORDER BY occurred_at ASC, rowid ASC
                   LIMIT ?""",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
        return [self._row_to_envelope(r) for r in rows]

    async def find_by_run(
        self,
        run_id: str,
        after_event_id: str | None = None,
        limit: int = 100,
    ) -> list[DomainEvent]:
        """按 ``run_id`` 查询事件流。"""
        return await self._find_by_scope("run_id", run_id, after_event_id, limit)

    async def find_by_project(
        self,
        project_id: str,
        after_event_id: str | None = None,
        limit: int = 100,
    ) -> list[DomainEvent]:
        """按 ``project_id`` 查询事件流。"""
        return await self._find_by_scope("project_id", project_id, after_event_id, limit)

    async def subscribe_run(
        self,
        run_id: str,
        after_event_id: str | None = None,
        limit: int = 100,
    ) -> AsyncIterator[DomainEvent]:
        """异步迭代 ``run_id`` 的事件流（一次性回放，不阻塞等待新事件）。"""
        events = await self.find_by_run(run_id, after_event_id, limit)
        for event in events:
            yield event

    async def get_event(self, event_id: str) -> DomainEvent | None:
        """按 ``event_id`` 取单条事件。"""
        async with self._database.read_connection() as conn:
            async with conn.execute(
                """SELECT id, event_type, schema_version, aggregate_type, aggregate_id,
                          organization_id, project_id, run_id, occurred_at,
                          actor_type, actor_id, trace_id, payload
                   FROM outbox_events
                   WHERE id = ?""",
                (event_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return self._row_to_event(row)

    # ------------------------------------------------------------------ #
    # 发布标记
    # ------------------------------------------------------------------ #

    async def mark_published(self, event_id: str) -> None:
        """标记事件已发布。"""
        now = _now_iso()
        async with self._database.write_connection() as conn:
            await conn.execute(
                "UPDATE outbox_events SET published_at = ?, last_error = NULL WHERE id = ?",
                (now, event_id),
            )

    async def mark_failed(self, event_id: str, error: str) -> None:
        """记录发布失败，递增重试计数。"""
        async with self._database.write_connection() as conn:
            await conn.execute(
                "UPDATE outbox_events SET publish_attempts = publish_attempts + 1, "
                "last_error = ? WHERE id = ?",
                (error, event_id),
            )

    # ------------------------------------------------------------------ #
    # 消费幂等
    # ------------------------------------------------------------------ #

    async def mark_consumed(self, event_id: str, consumer_name: str) -> bool:
        """幂等记录消费；返回是否首次消费。"""
        now = _now_iso()
        async with self._database.write_connection() as conn:
            cur = await conn.execute(
                "INSERT OR IGNORE INTO outbox_consumptions "
                "(event_id, consumer_name, consumed_at) VALUES (?, ?, ?)",
                (event_id, consumer_name, now),
            )
            inserted = cur.rowcount == 1
            await cur.close()
        return inserted

    async def has_consumed(self, event_id: str, consumer_name: str) -> bool:
        """检查是否已消费。"""
        async with self._database.read_connection() as conn:
            async with conn.execute(
                "SELECT 1 FROM outbox_consumptions WHERE event_id = ? AND consumer_name = ?",
                (event_id, consumer_name),
            ) as cur:
                row = await cur.fetchone()
        return row is not None

    async def consume(
        self,
        event_id: str,
        consumer_name: str,
        handler: EventHandler,
    ) -> bool:
        """幂等消费事件。

        流程：
            1. 已消费 → 直接返回 ``False``（无副作用）；
            2. 取事件，不存在抛 ``KeyError``；
            3. 执行 handler；handler 抛异常则不标记消费，向上抛出供调用方重试；
            4. handler 成功 → ``mark_consumed``；返回是否首次消费。

        重复消费保证（设计文档 §10.8）：``outbox_consumptions`` 以
        ``(event_id, consumer_name)`` 为主键，``INSERT OR IGNORE`` 保证只有一个
        调用者真正完成标记；后续重试因记录已存在而跳过 handler。
        """
        if await self.has_consumed(event_id, consumer_name):
            return False
        event = await self.get_event(event_id)
        if event is None:
            raise KeyError(f"事件 {event_id!r} 不存在于 outbox_events")
        await handler(event)  # 抛异常则不标记消费，调用方可重试
        return await self.mark_consumed(event_id, consumer_name)

    async def publish_pending(
        self,
        handler: EventHandler | None = None,
        batch_size: int = 100,
    ) -> int:
        """租用未发布事件并调用 handler；成功标记 published，失败标记 failed。

        失败时不抛异常，而是记录 ``last_error`` 并递增 ``publish_attempts``，
        下一轮可再次取到该事件重试。
        """
        envelopes = await self.list_unpublished(limit=batch_size)
        published = 0
        for envelope in envelopes:
            event = envelope.event
            try:
                if handler is not None:
                    await handler(event)
                await self.mark_published(event.event_id)
                published += 1
            except Exception as exc:  # noqa: BLE001 -- 重试由调度器决定
                await self.mark_failed(event.event_id, str(exc))
        return published

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    async def _find_by_scope(
        self,
        scope_column: str,
        scope_value: str,
        after_event_id: str | None,
        limit: int,
    ) -> list[DomainEvent]:
        """按作用域列查询事件流，支持游标续传。"""
        if scope_column not in ("run_id", "project_id"):
            raise ValueError(f"非法作用域列: {scope_column!r}")
        if after_event_id is None:
            sql = (
                f"SELECT id, event_type, schema_version, aggregate_type, aggregate_id,"
                f" organization_id, {scope_column}, run_id, occurred_at,"
                f" actor_type, actor_id, trace_id, payload"
                f" FROM outbox_events"
                f" WHERE {scope_column} = ?"
                f" ORDER BY rowid ASC LIMIT ?"
            )
            params: tuple[object, ...] = (scope_value, limit)
        else:
            sql = (
                f"SELECT id, event_type, schema_version, aggregate_type, aggregate_id,"
                f" organization_id, {scope_column}, run_id, occurred_at,"
                f" actor_type, actor_id, trace_id, payload"
                f" FROM outbox_events"
                f" WHERE {scope_column} = ?"
                f" AND rowid > (SELECT rowid FROM outbox_events WHERE id = ?)"
                f" ORDER BY rowid ASC LIMIT ?"
            )
            params = (scope_value, after_event_id, limit)
        async with self._database.read_connection() as conn:
            async with conn.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row: aiosqlite.Row | tuple) -> DomainEvent:
        """把 SELECT 行（13 列，不含 published_at/publish_attempts/last_error）转为事件。"""
        (
            event_id,
            event_type,
            schema_version,
            aggregate_type,
            aggregate_id,
            organization_id,
            project_id,
            run_id,
            occurred_at,
            actor_type,
            actor_id,
            trace_id,
            payload,
        ) = tuple(row)
        return DomainEvent(
            event_id=event_id,
            event_type=event_type,
            schema_version=schema_version,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            organization_id=organization_id,
            project_id=project_id,
            run_id=run_id,
            occurred_at=datetime.fromisoformat(occurred_at),
            actor=ActorRef(actor_type=actor_type, actor_id=actor_id),
            trace_id=trace_id or "",
            payload=json.loads(payload) if payload else {},
        )

    @classmethod
    def _row_to_envelope(cls, row: aiosqlite.Row | tuple) -> EventEnvelope:
        """把 SELECT 行（16 列）转为 EventEnvelope。"""
        full = tuple(row)
        event_tuple = full[:13]
        published_at = full[13]
        publish_attempts = full[14]
        last_error = full[15]
        return EventEnvelope(
            event=cls._row_to_event(event_tuple),
            published_at=datetime.fromisoformat(published_at) if published_at else None,
            publish_attempts=int(publish_attempts),
            last_error=last_error,
        )


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# TASK-021：事件幂等与判定记录编排
#
# 本节在 Outbox 消费幂等（``OutboxRepository.consume`` 以
# ``(consumer_name, event_id)`` 为幂等键）之上，为 Git coordination 事件处理
# 提供“先查重 → 处理 → 记录判定”的编排辅助。与 ``outbox_consumptions`` 的
# 区别：``event_decisions`` 额外记录判定类别（applied/skipped/failed）、结果
# 摘要、错误信息与内容哈希，支持冲突检测与失败重试（《TASK-021》验收）。
#
# 这里只编排判定记录，不修改事件内容（事件本身是 Git coordination 事实源）。
# --------------------------------------------------------------------------- #


class EventConsumer(Protocol):
    """事件处理判定仓库协议（``EventDecisionRepository`` 的最小接口）。

    谁实现它：
        ``maf_server.modules.git_coordination.repository.EventDecisionRepository``
        是 SQLite 实现，绑定到 ``UnitOfWork`` 事务连接。

    事务边界：
        所有方法在调用方提供的 ``UnitOfWork`` 事务内执行，随 ``commit``/``rollback``
        原子提交或回滚。``has_processed`` 只读，``record_decision`` 写入
        ``event_decisions`` 表。
    """

    async def has_processed(self, event_id: str, consumer_id: str) -> bool:
        """检查 ``(event_id, consumer_id)`` 是否已成功处理（非 failed）。"""
        ...

    async def record_decision(
        self,
        event_id: str,
        consumer_id: str,
        decision: str,
        result: str | None,
        error: str | None,
        content_hash: str = "",
    ) -> None:
        """记录事件处理判定；幂等。"""
        ...


async def has_processed_event(
    event_id: str,
    *,
    consumer_id: str,
    repository: EventConsumer,
) -> bool:
    """检查 ``event_id`` 是否已被 ``consumer_id`` 成功处理。

    在 ``UnitOfWork`` 事务内查询 ``event_decisions`` 表。``failed`` 判定视为
    未完成（可重试），返回 ``False``。

    :param event_id: 事件全局唯一 ID。
    :param consumer_id: 消费者标识。
    :param repository: 绑定到当前 UoW 事务连接的判定仓库。
    :returns: 已成功处理返回 ``True``，否则 ``False``。
    """
    return await repository.has_processed(event_id, consumer_id)


async def record_event_decision(
    event_id: str,
    *,
    consumer_id: str,
    decision: str,
    result: str | None = None,
    error: str | None = None,
    content_hash: str = "",
    repository: EventConsumer,
) -> None:
    """记录事件处理判定（PROCESSED/SKIPPED/FAILED）；幂等。

    在 ``UnitOfWork`` 事务内写入 ``event_decisions`` 表。幂等策略：

    - 同 ``(event_id, consumer_id)`` 同内容（``content_hash`` 一致）且原判定非
      ``failed`` → 幂等无操作，保留首次决定；
    - 同 ``(event_id, consumer_id)`` 同内容且原判定为 ``failed`` → 覆盖更新
      （支持失败重试）；
    - 同 ``(event_id, consumer_id)`` 不同内容 → 抛 ``IdempotencyConflictError``。

    本函数不修改事件内容，只记录判定。

    :param event_id: 事件全局唯一 ID。
    :param consumer_id: 消费者标识。
    :param decision: 判定值（``applied``/``skipped_duplicate``/``skipped_invalid``/
        ``failed``）。
    :param result: 处理结果摘要（如影响的 task_id、新状态），可空。
    :param error: 失败时的错误信息，``None`` 表示无错误。
    :param content_hash: 事件内容哈希，用于冲突检测；空字符串关闭冲突检测。
    :param repository: 绑定到当前 UoW 事务连接的判定仓库。
    """
    await repository.record_decision(
        event_id,
        consumer_id,
        decision,
        result,
        error,
        content_hash=content_hash,
    )


#: 事件处理回调类型：接收事件 dict，返回结果摘要字符串；抛异常表示处理失败。
#:
#: 与 :data:`EventHandler`（接收 ``DomainEvent``）不同，本回调接收 Git
#: coordination 事件（``CoordinationEvent`` dict），因为判定记录面向的是跨节点
#: coordination 事件的处理流程。
CoordinationEventHandler = Callable[[dict], Awaitable[str]]


async def process_event_idempotently(
    event: dict,
    *,
    consumer_id: str,
    repository: EventConsumer,
    handler: CoordinationEventHandler,
    compute_hash: Callable[[dict], str] | None = None,
) -> str:
    """编排“查重 → 处理 → 记录判定”的幂等事件处理流程。

    流程：

    1. 计算 ``content_hash``（默认用 ``compute_event_content_hash``）；
    2. 若 ``has_processed_event`` 返回 ``True`` → 记录 ``skipped_duplicate``
       并返回首次决定的结果摘要（不调用 handler，无副作用）；
    3. 否则调用 ``handler`` 处理事件：
       - handler 成功 → 记录 ``applied``，返回结果摘要；
       - handler 抛异常 → 记录 ``failed``（含错误信息）后重新抛出，调用方可重试。

    事务语义：

    - 成功路径：``applied`` 判定与 handler 的业务副作用在同一 ``UnitOfWork``
      事务内原子提交（调用方 ``commit``），重启后 ``has_processed_event``
      返回 ``True``，不重复应用状态变化（《TASK-021》验收）。
    - 失败路径：``failed`` 判定写入当前事务后重新抛出异常；若异常未在事务内
      捕获，``UnitOfWork`` 回滚将连同 ``failed`` 记录一并撤销（此时重试因
      ``has_processed_event`` 返回 ``False`` 而直接重跑 handler）。若调用方
      需持久化 ``failed`` 跟踪记录（用于审计/重试计数），应在事务内捕获异常
      并 ``commit``。
    - 整个流程应在同一 ``UnitOfWork`` 事务内执行，保证查重、处理副作用与判定
      记录原子提交。

    :param event: ``CoordinationEvent`` dict，必须含 ``event_id``。
    :param consumer_id: 消费者标识。
    :param repository: 绑定到当前 UoW 事务连接的判定仓库。
    :param handler: 事件处理回调，返回结果摘要字符串。
    :param compute_hash: 自定义内容哈希函数，默认用
        :func:`compute_event_content_hash`。
    :returns: 处理结果摘要（handler 返回值或首次决定的结果）。
    """
    event_id = event["event_id"]
    content_hash = compute_hash(event) if compute_hash is not None else _default_hash(event)

    if await has_processed_event(event_id, consumer_id=consumer_id, repository=repository):
        record = await _get_decision(repository, event_id, consumer_id)
        # ``get_decision`` is an optional repository extension, so keep the
        # core protocol agnostic and read the summary defensively.
        result_summary = (
            getattr(record, "result", None) if record is not None else None
        )
        await record_event_decision(
            event_id,
            consumer_id=consumer_id,
            decision="skipped_duplicate",
            result=result_summary,
            content_hash=content_hash,
            repository=repository,
        )
        return result_summary or ""

    try:
        result = await handler(event)
    except Exception as exc:
        await record_event_decision(
            event_id,
            consumer_id=consumer_id,
            decision="failed",
            result=None,
            error=str(exc),
            content_hash=content_hash,
            repository=repository,
        )
        raise

    await record_event_decision(
        event_id,
        consumer_id=consumer_id,
        decision="applied",
        result=result,
        content_hash=content_hash,
        repository=repository,
    )
    return result


async def _get_decision(
    repository: EventConsumer,
    event_id: str,
    consumer_id: str,
) -> object | None:
    """读取已有判定记录（若仓库支持 ``get_decision``）。"""
    getter = getattr(repository, "get_decision", None)
    if getter is None:
        return None
    return await getter(event_id, consumer_id)


def _default_hash(event: dict[str, Any]) -> str:
    """默认内容哈希：``compute_event_content_hash`` 的延迟导入包装。

    ``compute_event_content_hash`` 位于 ``git_coordination.repository``，为避免
    ``core.events`` → ``modules.git_coordination`` 的跨层导入循环，此处延迟
    导入。``core`` 层不依赖 ``modules`` 层是既定架构约束。
    """
    from maf_server.modules.git_coordination.repository import (
        compute_event_content_hash,
    )

    return compute_event_content_hash(cast(Any, event))


__all__ = [
    "EventPublisher",
    "SqliteEventPublisher",
    "OutboxRepository",
    "SqliteOutboxRepository",
    "EventHandler",
    "init_outbox_schema",
    "OUTBOX_EVENTS_DDL",
    "OUTBOX_INDEXES_DDL",
    "OUTBOX_CONSUMPTIONS_DDL",
    "EventConsumer",
    "CoordinationEventHandler",
    "has_processed_event",
    "record_event_decision",
    "process_event_idempotently",
]
