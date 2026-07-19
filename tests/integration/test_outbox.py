"""TASK-009 集成测试：Outbox 与领域事件。

验收标准：
1. 业务修改与 Outbox 在同一事务提交（commit 后两者均可见；rollback 后两者均不写）。
2. 消费失败可重试且不会重复产生投影副作用（``outbox_consumptions`` 幂等键去重）。
3. 事件可按 run/project 查询。

测试范围：
- ``apps/server/src/maf_server/core/events.py``：``SqliteEventPublisher``、
  ``SqliteOutboxRepository``、``init_outbox_schema``、``consume`` 幂等机制。
- ``packages/contracts_py/src/maf_contracts/events.py``：``DomainEvent``、
  ``EventEnvelope``、``ActorRef``。
- ``apps/server/src/maf_server/core/unit_of_work.py``：``SqliteUnitOfWork``（事务边界）。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from maf_contracts.events import ActorRef, DomainEvent, EventEnvelope
from maf_server.config import ServerSettings
from maf_server.core.database import Database
from maf_server.core.events import (
    SqliteEventPublisher,
    SqliteOutboxRepository,
    init_outbox_schema,
)
from maf_server.core.unit_of_work import SqliteUnitOfWork

_SECRET_PLAINTEXT = "test-secret-for-outbox-task-009"


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


@pytest_asyncio.fixture
async def db_with_outbox(db: Database) -> Database:
    """已创建 outbox_events 与 outbox_consumptions 表的 Database。"""
    await init_outbox_schema(db)
    return db


def _make_event(
    event_id: str | None = None,
    *,
    event_type: str = "task.created",
    aggregate_type: str = "Task",
    aggregate_id: str | None = None,
    organization_id: str = "org-001",
    project_id: str | None = "proj-001",
    run_id: str | None = "run-001",
    payload: dict | None = None,
    actor_type: str = "SERVICE",
    actor_id: str = "scheduler",
    trace_id: str = "trace-001",
) -> DomainEvent:
    """构造测试用 DomainEvent。"""
    return DomainEvent(
        event_id=event_id or f"evt-{uuid4()}",
        event_type=event_type,
        schema_version=1,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id or f"agg-{uuid4()}",
        organization_id=organization_id,
        project_id=project_id,
        run_id=run_id,
        occurred_at=datetime.now(timezone.utc),
        actor=ActorRef(actor_type=actor_type, actor_id=actor_id),
        trace_id=trace_id,
        payload=payload or {"key": "value"},
    )


async def _create_business_table(db: Database, table: str = "_biz") -> None:
    """创建业务测试表，模拟应用服务层的业务写入。"""
    async with db.write_connection() as conn:
        await conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            "id INTEGER PRIMARY KEY, val TEXT, version_no INTEGER NOT NULL DEFAULT 1)"
        )


async def _count_rows(db: Database, table: str) -> int:
    """统计表中行数。"""
    async with db.read_connection() as conn:
        async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else 0


# --------------------------------------------------------------------------- #
# 验收 1：业务修改与 Outbox 在同一事务提交
# --------------------------------------------------------------------------- #


class TestSameTransactionAsBusinessWrite:
    """业务修改与 Outbox 必须在同一事务内提交/回滚。"""

    @pytest.mark.asyncio
    async def test_commit_persists_both_business_and_outbox(self, db_with_outbox: Database) -> None:
        """commit 后业务行与 outbox 事件均可见。"""
        db = db_with_outbox
        await _create_business_table(db)
        event = _make_event("evt-commit-1")

        async with SqliteUnitOfWork(db) as uow:
            await uow.connection.execute("INSERT INTO _biz (id, val) VALUES (1, 'a')")
            publisher = SqliteEventPublisher(uow.connection)
            await publisher.append(event)
            await uow.commit()

        assert await _count_rows(db, "_biz") == 1
        assert await _count_rows(db, "outbox_events") == 1

        repo = SqliteOutboxRepository(db)
        fetched = await repo.get_event("evt-commit-1")
        assert fetched is not None
        assert fetched.event_type == event.event_type
        assert fetched.aggregate_type == event.aggregate_type
        assert fetched.payload == event.payload

    @pytest.mark.asyncio
    async def test_rollback_rolls_back_both_business_and_outbox(
        self, db_with_outbox: Database
    ) -> None:
        """异常或未 commit 时业务行与 outbox 事件均不写入。"""
        db = db_with_outbox
        await _create_business_table(db)
        event = _make_event("evt-rollback-1")

        with pytest.raises(RuntimeError, match="boom"):
            async with SqliteUnitOfWork(db) as uow:
                await uow.connection.execute("INSERT INTO _biz (id, val) VALUES (2, 'b')")
                publisher = SqliteEventPublisher(uow.connection)
                await publisher.append(event)
                raise RuntimeError("boom")

        # 业务表与 outbox 都不应有数据
        assert await _count_rows(db, "_biz") == 0
        assert await _count_rows(db, "outbox_events") == 0

    @pytest.mark.asyncio
    async def test_no_commit_rolls_back_automatically(self, db_with_outbox: Database) -> None:
        """进入 UoW 但未调用 commit 退出时自动回滚。"""
        db = db_with_outbox
        await _create_business_table(db)
        event = _make_event("evt-nocommit-1")

        async with SqliteUnitOfWork(db) as uow:
            await uow.connection.execute("INSERT INTO _biz (id, val) VALUES (3, 'c')")
            publisher = SqliteEventPublisher(uow.connection)
            await publisher.append(event)
            # 故意不调用 commit

        assert await _count_rows(db, "_biz") == 0
        assert await _count_rows(db, "outbox_events") == 0

    @pytest.mark.asyncio
    async def test_event_id_unique_violation(self, db_with_outbox: Database) -> None:
        """重复 event_id 在同一事务内触发 IntegrityError 并导致回滚。"""
        db = db_with_outbox
        event = _make_event("evt-dup-1")

        # 先成功写入一条
        async with SqliteUnitOfWork(db) as uow:
            publisher = SqliteEventPublisher(uow.connection)
            await publisher.append(event)
            await uow.commit()
        assert await _count_rows(db, "outbox_events") == 1

        # 再次 append 同一 event_id 应抛 IntegrityError
        with pytest.raises(sqlite3.IntegrityError):
            async with SqliteUnitOfWork(db) as uow:
                publisher = SqliteEventPublisher(uow.connection)
                await publisher.append(event)
                await uow.commit()

        # 原始那条仍在
        assert await _count_rows(db, "outbox_events") == 1

    @pytest.mark.asyncio
    async def test_multiple_events_same_transaction_atomic(self, db_with_outbox: Database) -> None:
        """同一事务内追加多个事件，commit 后全部可见；rollback 全部消失。"""
        db = db_with_outbox
        events = [_make_event(f"evt-multi-{i}") for i in range(3)]

        async with SqliteUnitOfWork(db) as uow:
            publisher = SqliteEventPublisher(uow.connection)
            for event in events:
                await publisher.append(event)
            await uow.commit()

        assert await _count_rows(db, "outbox_events") == 3

        # 再追加一批但 rollback
        more_events = [_make_event(f"evt-multi-rb-{i}") for i in range(2)]
        with pytest.raises(RuntimeError, match="rollback-batch"):
            async with SqliteUnitOfWork(db) as uow:
                publisher = SqliteEventPublisher(uow.connection)
                for event in more_events:
                    await publisher.append(event)
                raise RuntimeError("rollback-batch")

        # 仍是 3 条
        assert await _count_rows(db, "outbox_events") == 3


# --------------------------------------------------------------------------- #
# 验收 2：消费失败可重试且不会重复产生投影副作用
# --------------------------------------------------------------------------- #


class TestConsumeIdempotency:
    """Outbox 消费幂等：失败可重试，成功后重复消费不重复副作用。"""

    @pytest.mark.asyncio
    async def test_consume_success_marks_consumed(self, db_with_outbox: Database) -> None:
        """首次消费成功后标记 consumed，第二次 consume 跳过 handler。"""
        db = db_with_outbox
        event = _make_event("evt-consume-ok")
        async with SqliteUnitOfWork(db) as uow:
            await SqliteEventPublisher(uow.connection).append(event)
            await uow.commit()

        repo = SqliteOutboxRepository(db)
        call_count = 0

        async def handler(_event: DomainEvent) -> None:
            nonlocal call_count
            call_count += 1

        # 首次消费
        first = await repo.consume("evt-consume-ok", "consumer-A", handler)
        assert first is True
        assert call_count == 1
        assert await repo.has_consumed("evt-consume-ok", "consumer-A") is True

        # 第二次消费：handler 不应被调用
        second = await repo.consume("evt-consume-ok", "consumer-A", handler)
        assert second is False
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_consume_failure_does_not_mark_then_retry_succeeds(
        self, db_with_outbox: Database
    ) -> None:
        """handler 首次失败不标记 consumed，重试成功后标记，再重复消费无副作用。"""
        db = db_with_outbox
        event = _make_event("evt-consume-retry")
        async with SqliteUnitOfWork(db) as uow:
            await SqliteEventPublisher(uow.connection).append(event)
            await uow.commit()

        # 投影表（模拟消费副作用）
        async with db.write_connection() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS _proj (event_id TEXT PRIMARY KEY, val TEXT)"
            )

        repo = SqliteOutboxRepository(db)
        call_count = 0

        async def handler(ev: DomainEvent) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient failure")
            # 成功时幂等写入投影（event_id 为主键，避免重复副作用）
            async with db.write_connection() as conn:
                await conn.execute(
                    "INSERT OR IGNORE INTO _proj (event_id, val) VALUES (?, ?)",
                    (ev.event_id, "done"),
                )

        # 首次消费失败
        with pytest.raises(RuntimeError, match="transient failure"):
            await repo.consume("evt-consume-retry", "consumer-B", handler)
        assert call_count == 1
        assert await repo.has_consumed("evt-consume-retry", "consumer-B") is False
        assert await _count_rows(db, "_proj") == 0

        # 重试成功
        second = await repo.consume("evt-consume-retry", "consumer-B", handler)
        assert second is True
        assert call_count == 2
        assert await repo.has_consumed("evt-consume-retry", "consumer-B") is True
        assert await _count_rows(db, "_proj") == 1

        # 再次 consume：已消费，handler 不执行，投影仍为 1
        third = await repo.consume("evt-consume-retry", "consumer-B", handler)
        assert third is False
        assert call_count == 2
        assert await _count_rows(db, "_proj") == 1

    @pytest.mark.asyncio
    async def test_different_consumers_independent(self, db_with_outbox: Database) -> None:
        """同一事件可被不同 consumer 各自消费一次。"""
        db = db_with_outbox
        event = _make_event("evt-multi-consumer")
        async with SqliteUnitOfWork(db) as uow:
            await SqliteEventPublisher(uow.connection).append(event)
            await uow.commit()

        repo = SqliteOutboxRepository(db)
        calls: list[str] = []

        async def handler_a(ev: DomainEvent) -> None:
            calls.append(f"A:{ev.event_id}")

        async def handler_b(ev: DomainEvent) -> None:
            calls.append(f"B:{ev.event_id}")

        assert await repo.consume("evt-multi-consumer", "A", handler_a) is True
        assert await repo.consume("evt-multi-consumer", "B", handler_b) is True
        # 各自重复消费不应再触发 handler
        assert await repo.consume("evt-multi-consumer", "A", handler_a) is False
        assert await repo.consume("evt-multi-consumer", "B", handler_b) is False

        assert calls == ["A:evt-multi-consumer", "B:evt-multi-consumer"]

    @pytest.mark.asyncio
    async def test_mark_consumed_returns_false_on_duplicate(self, db_with_outbox: Database) -> None:
        """mark_consumed 首次返回 True，重复返回 False。"""
        db = db_with_outbox
        repo = SqliteOutboxRepository(db)

        assert await repo.mark_consumed("evt-x", "C") is True
        assert await repo.mark_consumed("evt-x", "C") is False
        assert await repo.has_consumed("evt-x", "C") is True

    @pytest.mark.asyncio
    async def test_consume_unknown_event_raises_keyerror(self, db_with_outbox: Database) -> None:
        """消费不存在的事件抛 KeyError。"""
        repo = SqliteOutboxRepository(db_with_outbox)

        async def handler(_ev: DomainEvent) -> None:
            pass

        with pytest.raises(KeyError, match="不存在"):
            await repo.consume("evt-does-not-exist", "C", handler)


# --------------------------------------------------------------------------- #
# 验收 3：事件可按 run/project 查询
# --------------------------------------------------------------------------- #


class TestQueryByRunAndProject:
    """按 run_id / project_id 查询事件流。"""

    @pytest.mark.asyncio
    async def test_find_by_run_filters_correctly(self, db_with_outbox: Database) -> None:
        """find_by_run 只返回匹配 run_id 的事件，按插入顺序升序。"""
        db = db_with_outbox
        async with SqliteUnitOfWork(db) as uow:
            publisher = SqliteEventPublisher(uow.connection)
            await publisher.append(_make_event("r1-e1", run_id="run-A"))
            await publisher.append(_make_event("r2-e1", run_id="run-B"))
            await publisher.append(_make_event("r1-e2", run_id="run-A"))
            await publisher.append(_make_event("r1-e3", run_id="run-A"))
            await uow.commit()

        repo = SqliteOutboxRepository(db)
        events = await repo.find_by_run("run-A")
        assert [e.event_id for e in events] == ["r1-e1", "r1-e2", "r1-e3"]
        for e in events:
            assert e.run_id == "run-A"

    @pytest.mark.asyncio
    async def test_find_by_project_filters_correctly(self, db_with_outbox: Database) -> None:
        """find_by_project 只返回匹配 project_id 的事件。"""
        db = db_with_outbox
        async with SqliteUnitOfWork(db) as uow:
            publisher = SqliteEventPublisher(uow.connection)
            await publisher.append(_make_event("p1-e1", project_id="proj-A"))
            await publisher.append(_make_event("p2-e1", project_id="proj-B"))
            await publisher.append(_make_event("p1-e2", project_id="proj-A"))
            await uow.commit()

        repo = SqliteOutboxRepository(db)
        events = await repo.find_by_project("proj-A")
        assert [e.event_id for e in events] == ["p1-e1", "p1-e2"]
        for e in events:
            assert e.project_id == "proj-A"

    @pytest.mark.asyncio
    async def test_find_by_run_after_event_id_cursor(self, db_with_outbox: Database) -> None:
        """after_event_id 作为游标，返回该事件之后的事件。"""
        db = db_with_outbox
        async with SqliteUnitOfWork(db) as uow:
            publisher = SqliteEventPublisher(uow.connection)
            for i in range(5):
                await publisher.append(_make_event(f"cursor-{i}", run_id="run-C"))
            await uow.commit()

        repo = SqliteOutboxRepository(db)
        first_batch = await repo.find_by_run("run-C", limit=2)
        assert [e.event_id for e in first_batch] == ["cursor-0", "cursor-1"]

        # 从 cursor-1 之后继续
        second_batch = await repo.find_by_run("run-C", after_event_id="cursor-1", limit=10)
        assert [e.event_id for e in second_batch] == [
            "cursor-2",
            "cursor-3",
            "cursor-4",
        ]

    @pytest.mark.asyncio
    async def test_find_by_run_empty(self, db_with_outbox: Database) -> None:
        """不存在的 run_id 返回空列表。"""
        repo = SqliteOutboxRepository(db_with_outbox)
        assert await repo.find_by_run("run-nonexistent") == []

    @pytest.mark.asyncio
    async def test_find_by_project_empty(self, db_with_outbox: Database) -> None:
        """不存在的 project_id 返回空列表。"""
        repo = SqliteOutboxRepository(db_with_outbox)
        assert await repo.find_by_project("proj-nonexistent") == []

    @pytest.mark.asyncio
    async def test_subscribe_run_yields_in_order(self, db_with_outbox: Database) -> None:
        """subscribe_run 异步迭代按插入顺序产出事件。"""
        db = db_with_outbox
        async with SqliteUnitOfWork(db) as uow:
            publisher = SqliteEventPublisher(uow.connection)
            for i in range(3):
                await publisher.append(_make_event(f"sub-{i}", run_id="run-D"))
            await uow.commit()

        repo = SqliteOutboxRepository(db)
        collected: list[str] = []
        async for event in repo.subscribe_run("run-D"):
            collected.append(event.event_id)
        assert collected == ["sub-0", "sub-1", "sub-2"]

    @pytest.mark.asyncio
    async def test_null_run_or_project_not_matched(self, db_with_outbox: Database) -> None:
        """run_id/project_id 为 NULL 的事件不会被 run/project 查询匹配。"""
        db = db_with_outbox
        async with SqliteUnitOfWork(db) as uow:
            publisher = SqliteEventPublisher(uow.connection)
            await publisher.append(_make_event("null-1", run_id=None, project_id=None))
            await uow.commit()

        repo = SqliteOutboxRepository(db)
        assert await repo.find_by_run("run-A") == []
        assert await repo.find_by_project("proj-A") == []
        # 但事件确实存在
        assert await repo.get_event("null-1") is not None


# --------------------------------------------------------------------------- #
# 发布标记与重试
# --------------------------------------------------------------------------- #


class TestPublishMarkingAndRetry:
    """Outbox 发布标记、重试计数与 publish_pending 批处理。"""

    @pytest.mark.asyncio
    async def test_list_unpublished_in_order(self, db_with_outbox: Database) -> None:
        """未发布事件按 occurred_at 升序返回。"""
        db = db_with_outbox
        async with SqliteUnitOfWork(db) as uow:
            publisher = SqliteEventPublisher(uow.connection)
            await publisher.append(_make_event("u1"))
            await publisher.append(_make_event("u2"))
            await publisher.append(_make_event("u3"))
            await uow.commit()

        repo = SqliteOutboxRepository(db)
        envelopes = await repo.list_unpublished()
        assert len(envelopes) == 3
        assert [e.event.event_id for e in envelopes] == ["u1", "u2", "u3"]
        # 全部应为 PENDING 状态
        for env in envelopes:
            assert env.status == "PENDING"
            assert env.published_at is None
            assert env.publish_attempts == 0

    @pytest.mark.asyncio
    async def test_mark_published_excludes_from_unpublished(self, db_with_outbox: Database) -> None:
        """mark_published 后该事件不再出现在 list_unpublished。"""
        db = db_with_outbox
        async with SqliteUnitOfWork(db) as uow:
            publisher = SqliteEventPublisher(uow.connection)
            await publisher.append(_make_event("p1"))
            await publisher.append(_make_event("p2"))
            await uow.commit()

        repo = SqliteOutboxRepository(db)
        await repo.mark_published("p1")

        envelopes = await repo.list_unpublished()
        assert [e.event.event_id for e in envelopes] == ["p2"]

        # 已发布事件仍可通过 get_event 取到
        published_event = await repo.get_event("p1")
        assert published_event is not None

    @pytest.mark.asyncio
    async def test_mark_failed_increments_attempts_and_error(
        self, db_with_outbox: Database
    ) -> None:
        """mark_failed 递增 publish_attempts 并写入 last_error。"""
        db = db_with_outbox
        async with SqliteUnitOfWork(db) as uow:
            await SqliteEventPublisher(uow.connection).append(_make_event("f1"))
            await uow.commit()

        repo = SqliteOutboxRepository(db)
        await repo.mark_failed("f1", "timeout")
        await repo.mark_failed("f1", "timeout-2")

        envelopes = await repo.list_unpublished()
        env = next(e for e in envelopes if e.event.event_id == "f1")
        assert env.publish_attempts == 2
        assert env.last_error == "timeout-2"
        assert env.status == "FAILED"
        # 失败的事件仍在未发布列表中（可重试）
        assert env.published_at is None

    @pytest.mark.asyncio
    async def test_mark_published_clears_last_error(self, db_with_outbox: Database) -> None:
        """mark_published 清空 last_error 并设置 published_at。"""
        db = db_with_outbox
        async with SqliteUnitOfWork(db) as uow:
            await SqliteEventPublisher(uow.connection).append(_make_event("f2"))
            await uow.commit()

        repo = SqliteOutboxRepository(db)
        await repo.mark_failed("f2", "err")
        await repo.mark_published("f2")

        envelopes = await repo.list_unpublished()
        assert not any(e.event.event_id == "f2" for e in envelopes)

    @pytest.mark.asyncio
    async def test_publish_pending_success_marks_published(self, db_with_outbox: Database) -> None:
        """publish_pending 成功调用 handler 后标记 published。"""
        db = db_with_outbox
        async with SqliteUnitOfWork(db) as uow:
            publisher = SqliteEventPublisher(uow.connection)
            await publisher.append(_make_event("pp-1"))
            await publisher.append(_make_event("pp-2"))
            await uow.commit()

        repo = SqliteOutboxRepository(db)
        seen: list[str] = []

        async def handler(ev: DomainEvent) -> None:
            seen.append(ev.event_id)

        published = await repo.publish_pending(handler=handler)
        assert published == 2
        assert seen == ["pp-1", "pp-2"]
        assert await repo.list_unpublished() == []

    @pytest.mark.asyncio
    async def test_publish_pending_failure_marks_failed_and_retryable(
        self, db_with_outbox: Database
    ) -> None:
        """handler 失败时该事件被标记 failed，下一轮可重试。"""
        db = db_with_outbox
        async with SqliteUnitOfWork(db) as uow:
            publisher = SqliteEventPublisher(uow.connection)
            await publisher.append(_make_event("pf-1"))
            await publisher.append(_make_event("pf-2"))
            await uow.commit()

        repo = SqliteOutboxRepository(db)
        attempt = 0

        async def handler(ev: DomainEvent) -> None:
            nonlocal attempt
            attempt += 1
            if ev.event_id == "pf-1" and attempt == 1:
                raise RuntimeError("transient")

        # 第一轮：pf-1 失败，pf-2 成功
        published = await repo.publish_pending(handler=handler)
        assert published == 1
        envelopes = await repo.list_unpublished()
        assert len(envelopes) == 1
        assert envelopes[0].event.event_id == "pf-1"
        assert envelopes[0].publish_attempts == 1
        assert "transient" in (envelopes[0].last_error or "")

        # 第二轮：pf-1 成功
        published2 = await repo.publish_pending(handler=handler)
        assert published2 == 1
        assert await repo.list_unpublished() == []

    @pytest.mark.asyncio
    async def test_publish_pending_no_handler_just_marks(self, db_with_outbox: Database) -> None:
        """无 handler 时 publish_pending 直接标记所有为 published。"""
        db = db_with_outbox
        async with SqliteUnitOfWork(db) as uow:
            publisher = SqliteEventPublisher(uow.connection)
            await publisher.append(_make_event("nh-1"))
            await uow.commit()

        repo = SqliteOutboxRepository(db)
        published = await repo.publish_pending(handler=None)
        assert published == 1
        assert await repo.list_unpublished() == []


# --------------------------------------------------------------------------- #
# EventEnvelope 与 DomainEvent 模型
# --------------------------------------------------------------------------- #


class TestEventModels:
    """DomainEvent 与 EventEnvelope 模型行为。"""

    def test_domain_event_defaults(self) -> None:
        """DomainEvent 默认 event_id 与 occurred_at 自动生成。"""
        event = DomainEvent(
            event_type="task.created",
            aggregate_type="Task",
            aggregate_id="agg-1",
            organization_id="org-1",
            actor=ActorRef(actor_type="SERVICE", actor_id="s"),
        )
        assert event.event_id  # 自动生成 UUID
        assert event.schema_version == 1
        assert event.project_id is None
        assert event.run_id is None
        assert event.payload == {}
        assert event.trace_id == ""
        assert event.occurred_at.tzinfo is not None  # 带时区

    def test_event_envelope_status_pending(self) -> None:
        """新 Envelope 状态为 PENDING。"""
        env = EventEnvelope(event=_make_event("s1"))
        assert env.status == "PENDING"
        assert env.publish_attempts == 0
        assert env.last_error is None
        assert env.published_at is None

    def test_event_envelope_status_failed(self) -> None:
        """有 last_error 且未发布 → FAILED。"""
        env = EventEnvelope(
            event=_make_event("s2"),
            last_error="timeout",
            publish_attempts=2,
        )
        assert env.status == "FAILED"

    def test_event_envelope_status_published(self) -> None:
        """published_at 非空 → PUBLISHED（即使有 last_error）。"""
        env = EventEnvelope(
            event=_make_event("s3"),
            published_at=datetime.now(timezone.utc),
            last_error="prior-err",
            publish_attempts=1,
        )
        assert env.status == "PUBLISHED"

    def test_domain_event_roundtrip_json(self) -> None:
        """DomainEvent 可 JSON 序列化/反序列化。"""
        import json

        event = _make_event("rt-1", payload={"n": 42, "s": "文本"})
        data = event.model_dump_json()
        restored = DomainEvent.model_validate_json(data)
        assert restored.event_id == event.event_id
        assert restored.payload == event.payload
        assert restored.actor.actor_type == event.actor.actor_type
        # occurred_at 应能往返
        assert restored.occurred_at == event.occurred_at
        # 确认 JSON 可解析
        json.loads(data)
