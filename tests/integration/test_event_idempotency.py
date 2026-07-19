"""TASK-021 集成测试：事件幂等与判定记录。

验收标准（对应《TASK-021》文档）：

1. **同 event_id 相同内容返回首次决定**：重复记录同内容事件时幂等，保留
   首次决定，``version_no`` 不递增。
2. **同 event_id 不同内容被判冲突**：相同 ``(event_id, consumer_id)`` 但
   ``content_hash`` 不同时抛 ``IdempotencyConflictError``。
3. **重启后不会重复应用状态变化**：``has_processed_event`` 检测到已处理后
   跳过 handler，不重复副作用。
4. **FAILED 决策可重试**：``failed`` 判定可被后续 ``applied`` 覆盖，
   ``has_processed_event`` 对 ``failed`` 返回 ``False``。
5. **事件内容不被修改**：判定记录流程不修改事件 dict 本身。
6. **不同 consumer 独立处理**：同一事件可被不同 consumer 各自记录一次。

测试范围：
- ``apps/server/src/maf_server/modules/git_coordination/repository.py``：
  ``EventDecisionRepository``、``init_event_decisions_schema``、
  ``compute_event_content_hash``、``event_decisions`` 表。
- ``apps/server/src/maf_server/core/events.py``：``has_processed_event``、
  ``record_event_decision``、``process_event_idempotently``、``EventConsumer``。
- ``apps/server/src/maf_server/core/unit_of_work.py``：``SqliteUnitOfWork``。
"""

from __future__ import annotations

import copy
import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from maf_domain.errors import IdempotencyConflictError
from maf_server.config import ServerSettings
from maf_server.core.database import Database
from maf_server.core.events import (
    has_processed_event,
    process_event_idempotently,
    record_event_decision,
)
from maf_server.core.unit_of_work import SqliteUnitOfWork
from maf_server.modules.git_coordination.repository import (
    EVENT_DECISION_APPLIED,
    EVENT_DECISION_FAILED,
    EVENT_DECISION_SKIPPED_DUPLICATE,
    EVENT_DECISION_SKIPPED_INVALID,
    EventDecisionRepository,
    compute_event_content_hash,
    init_event_decisions_schema,
)

_SECRET_PLAINTEXT = "test-secret-for-event-idempotency-task-021"


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
async def db_with_decisions(db: Database) -> Database:
    """已创建 event_decisions 表的 Database。"""
    await init_event_decisions_schema(db)
    return db


def _make_event(
    event_id: str | None = None,
    *,
    event_type: str = "CLAIM_REQUESTED",
    node_id: str = "node-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    task_id: str | None = "TASK-001",
    assignment_id: str | None = None,
    assignment_epoch: int | None = 1,
    based_on_control_commit: str = "abcdef1234567890abcdef1234567890abcdef12",
    occurred_at: str = "2026-07-17T00:00:00Z",
    payload: dict | None = None,
) -> dict:
    """构造一个合法的 CoordinationEvent dict。"""
    return {
        "schema_version": 1,
        "event_id": event_id or f"evt-{uuid.uuid4()}",
        "event_type": event_type,
        "node_id": node_id,
        "task_id": task_id,
        "assignment_id": assignment_id,
        "assignment_epoch": assignment_epoch,
        "based_on_control_commit": based_on_control_commit,
        "occurred_at": occurred_at,
        "payload": payload or {"note": "test event"},
    }


async def _record(
    db: Database,
    event_id: str,
    consumer_id: str,
    decision: str,
    *,
    result: str | None = None,
    error: str | None = None,
    content_hash: str = "",
) -> None:
    """在独立写事务内记录判定（便捷封装）。"""
    async with db.write_connection() as conn:
        repo = EventDecisionRepository(conn)
        await repo.record_decision(
            event_id, consumer_id, decision, result, error, content_hash=content_hash
        )


async def _has_processed(db: Database, event_id: str, consumer_id: str) -> bool:
    """在只读连接内查询是否已处理（便捷封装）。"""
    async with db.read_connection() as conn:
        repo = EventDecisionRepository(conn)
        return await repo.has_processed(event_id, consumer_id)


async def _get_decision(db: Database, event_id: str, consumer_id: str):
    async with db.read_connection() as conn:
        repo = EventDecisionRepository(conn)
        return await repo.get_decision(event_id, consumer_id)


# --------------------------------------------------------------------------- #
# 验收 1：同 event_id 相同内容返回首次决定（幂等）
# --------------------------------------------------------------------------- #


class TestIdempotentSameContent:
    """同 event_id 相同内容：幂等，保留首次决定。"""

    @pytest.mark.asyncio
    async def test_first_record_inserts(self, db_with_decisions: Database) -> None:
        db = db_with_decisions
        event = _make_event("evt-idem-001")
        content_hash = compute_event_content_hash(event)

        await _record(
            db,
            "evt-idem-001",
            "consumer-A",
            EVENT_DECISION_APPLIED,
            result="task=TASK-001,status=ASSIGNED",
            content_hash=content_hash,
        )

        record = await _get_decision(db, "evt-idem-001", "consumer-A")
        assert record is not None
        assert record.event_id == "evt-idem-001"
        assert record.consumer_id == "consumer-A"
        assert record.decision == EVENT_DECISION_APPLIED
        assert record.result == "task=TASK-001,status=ASSIGNED"
        assert record.error is None
        assert record.content_hash == content_hash
        assert record.version_no == 1
        assert record.is_processed is True

    @pytest.mark.asyncio
    async def test_duplicate_same_content_keeps_first_decision(
        self, db_with_decisions: Database
    ) -> None:
        """同内容重复记录：保留首次决定，version_no 不递增。"""
        db = db_with_decisions
        event = _make_event("evt-idem-002")
        content_hash = compute_event_content_hash(event)

        # 首次记录 applied
        await _record(
            db,
            "evt-idem-002",
            "consumer-A",
            EVENT_DECISION_APPLIED,
            result="first-result",
            content_hash=content_hash,
        )
        # 重复记录（同内容），传入不同 result/decision 也不应覆盖
        await _record(
            db,
            "evt-idem-002",
            "consumer-A",
            EVENT_DECISION_SKIPPED_DUPLICATE,
            result="second-result",
            content_hash=content_hash,
        )

        record = await _get_decision(db, "evt-idem-002", "consumer-A")
        assert record is not None
        # 保留首次决定
        assert record.decision == EVENT_DECISION_APPLIED
        assert record.result == "first-result"
        assert record.version_no == 1  # 未递增

    @pytest.mark.asyncio
    async def test_has_processed_true_after_applied(
        self, db_with_decisions: Database
    ) -> None:
        """applied 后 has_processed 返回 True。"""
        db = db_with_decisions
        event = _make_event("evt-idem-003")
        await _record(
            db,
            "evt-idem-003",
            "consumer-A",
            EVENT_DECISION_APPLIED,
            content_hash=compute_event_content_hash(event),
        )
        assert await _has_processed(db, "evt-idem-003", "consumer-A") is True

    @pytest.mark.asyncio
    async def test_has_processed_false_for_unknown(
        self, db_with_decisions: Database
    ) -> None:
        """未记录的事件 has_processed 返回 False。"""
        db = db_with_decisions
        assert await _has_processed(db, "evt-unknown", "consumer-A") is False


# --------------------------------------------------------------------------- #
# 验收 2：同 event_id 不同内容被判冲突
# --------------------------------------------------------------------------- #


class TestContentConflict:
    """同 (event_id, consumer_id) 但内容不同 → IdempotencyConflictError。"""

    @pytest.mark.asyncio
    async def test_different_content_raises_conflict(
        self, db_with_decisions: Database
    ) -> None:
        db = db_with_decisions
        event_v1 = _make_event("evt-conflict-001", payload={"v": 1})
        event_v2 = _make_event("evt-conflict-001", payload={"v": 2})
        hash_v1 = compute_event_content_hash(event_v1)
        hash_v2 = compute_event_content_hash(event_v2)
        assert hash_v1 != hash_v2  # 内容不同 → 哈希不同

        # 首次记录 v1
        await _record(
            db,
            "evt-conflict-001",
            "consumer-A",
            EVENT_DECISION_APPLIED,
            content_hash=hash_v1,
        )

        # 同 event_id 不同内容 → 冲突
        with pytest.raises(IdempotencyConflictError, match="不同内容"):
            await _record(
                db,
                "evt-conflict-001",
                "consumer-A",
                EVENT_DECISION_APPLIED,
                content_hash=hash_v2,
            )

        # 原记录保留
        record = await _get_decision(db, "evt-conflict-001", "consumer-A")
        assert record is not None
        assert record.content_hash == hash_v1
        assert record.decision == EVENT_DECISION_APPLIED

    @pytest.mark.asyncio
    async def test_no_content_hash_disables_conflict_check(
        self, db_with_decisions: Database
    ) -> None:
        """content_hash 为空时关闭冲突检测，幂等覆盖失败但不报冲突。"""
        db = db_with_decisions
        # 首次记录（无 hash）
        await _record(
            db,
            "evt-nohash-001",
            "consumer-A",
            EVENT_DECISION_APPLIED,
            content_hash="",
        )
        # 重复记录（无 hash）不抛冲突，幂等保留首次
        await _record(
            db,
            "evt-nohash-001",
            "consumer-A",
            EVENT_DECISION_APPLIED,
            content_hash="",
        )
        record = await _get_decision(db, "evt-nohash-001", "consumer-A")
        assert record is not None
        assert record.version_no == 1


# --------------------------------------------------------------------------- #
# 验收 3：重启后不会重复应用状态变化（has_processed 检测跳过）
# --------------------------------------------------------------------------- #


class TestProcessEventIdempotently:
    """process_event_idempotently 编排：查重 → 处理 → 记录判定。"""

    @pytest.mark.asyncio
    async def test_first_process_applies_and_records(
        self, db_with_decisions: Database
    ) -> None:
        db = db_with_decisions
        event = _make_event("evt-orch-001")
        side_effects: list[str] = []

        async def handler(ev: dict) -> str:
            side_effects.append(ev["event_id"])
            return f"applied:{ev['event_id']}"

        async with SqliteUnitOfWork(db) as uow:
            repo = EventDecisionRepository(uow.connection)
            result = await process_event_idempotently(
                event, consumer_id="projector-1", repository=repo, handler=handler
            )
            await uow.commit()

        assert result == "applied:evt-orch-001"
        assert side_effects == ["evt-orch-001"]
        assert await _has_processed(db, "evt-orch-001", "projector-1") is True
        record = await _get_decision(db, "evt-orch-001", "projector-1")
        assert record is not None
        assert record.decision == EVENT_DECISION_APPLIED
        assert record.result == "applied:evt-orch-001"

    @pytest.mark.asyncio
    async def test_duplicate_process_skips_handler(
        self, db_with_decisions: Database
    ) -> None:
        """重启/重复：has_processed=True 时跳过 handler，无副作用。"""
        db = db_with_decisions
        event = _make_event("evt-orch-002")
        call_count = 0

        async def handler(ev: dict) -> str:
            nonlocal call_count
            call_count += 1
            return f"applied:{ev['event_id']}"

        # 首次处理
        async with SqliteUnitOfWork(db) as uow:
            repo = EventDecisionRepository(uow.connection)
            await process_event_idempotently(
                event, consumer_id="projector-1", repository=repo, handler=handler
            )
            await uow.commit()
        assert call_count == 1

        # 重复处理：handler 不应被调用
        async with SqliteUnitOfWork(db) as uow:
            repo = EventDecisionRepository(uow.connection)
            result = await process_event_idempotently(
                event, consumer_id="projector-1", repository=repo, handler=handler
            )
            await uow.commit()
        assert call_count == 1  # 未再次调用
        assert result == "applied:evt-orch-002"  # 返回首次结果

        # 记录仍保留首次决定
        record = await _get_decision(db, "evt-orch-002", "projector-1")
        assert record is not None
        assert record.decision == EVENT_DECISION_APPLIED
        assert record.version_no == 1

    @pytest.mark.asyncio
    async def test_failed_then_retry_succeeds(self, db_with_decisions: Database) -> None:
        """handler 失败记录 failed；重试成功覆盖为 applied。

        失败时 ``process_event_idempotently`` 在当前事务写入 ``failed`` 判定后重新
        抛出异常。调用方在事务内捕获异常并提交，以持久化 ``failed`` 跟踪记录
        （业务副作用若在同一事务则随提交一并保留；此处 handler 无 DB 写入，
        提交是安全的）。
        """
        db = db_with_decisions
        event = _make_event("evt-orch-003")
        attempt = 0

        async def handler(ev: dict) -> str:
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise RuntimeError("transient failure")
            return f"applied:retry:{ev['event_id']}"

        # 首次处理失败：捕获异常以提交 failed 跟踪记录
        async with SqliteUnitOfWork(db) as uow:
            repo = EventDecisionRepository(uow.connection)
            with pytest.raises(RuntimeError, match="transient failure"):
                await process_event_idempotently(
                    event, consumer_id="projector-1", repository=repo, handler=handler
                )
            await uow.commit()  # 持久化 failed 判定

        assert attempt == 1
        # failed 记录已写入，has_processed 返回 False（可重试）
        assert await _has_processed(db, "evt-orch-003", "projector-1") is False
        record = await _get_decision(db, "evt-orch-003", "projector-1")
        assert record is not None
        assert record.decision == EVENT_DECISION_FAILED
        assert "transient failure" in (record.error or "")
        assert record.version_no == 1

        # 重试成功
        async with SqliteUnitOfWork(db) as uow:
            repo = EventDecisionRepository(uow.connection)
            result = await process_event_idempotently(
                event, consumer_id="projector-1", repository=repo, handler=handler
            )
            await uow.commit()
        assert attempt == 2
        assert result == "applied:retry:evt-orch-003"

        # 记录已覆盖为 applied
        record = await _get_decision(db, "evt-orch-003", "projector-1")
        assert record is not None
        assert record.decision == EVENT_DECISION_APPLIED
        assert record.result == "applied:retry:evt-orch-003"
        assert record.version_no == 2  # 递增
        assert record.error is None
        assert await _has_processed(db, "evt-orch-003", "projector-1") is True


# --------------------------------------------------------------------------- #
# 验收 4：不同 consumer 独立处理
# --------------------------------------------------------------------------- #


class TestDifferentConsumersIndependent:
    """同一事件可被不同 consumer 各自记录一次。"""

    @pytest.mark.asyncio
    async def test_different_consumers_each_process_once(
        self, db_with_decisions: Database
    ) -> None:
        db = db_with_decisions
        event = _make_event("evt-multi-consumer-001")
        content_hash = compute_event_content_hash(event)

        # consumer-A 处理
        await _record(
            db,
            "evt-multi-consumer-001",
            "consumer-A",
            EVENT_DECISION_APPLIED,
            result="A-result",
            content_hash=content_hash,
        )
        # consumer-B 处理（同一事件，不同 consumer）
        await _record(
            db,
            "evt-multi-consumer-001",
            "consumer-B",
            EVENT_DECISION_APPLIED,
            result="B-result",
            content_hash=content_hash,
        )

        assert await _has_processed(db, "evt-multi-consumer-001", "consumer-A") is True
        assert await _has_processed(db, "evt-multi-consumer-001", "consumer-B") is True
        # consumer-C 未处理
        assert await _has_processed(db, "evt-multi-consumer-001", "consumer-C") is False

        record_a = await _get_decision(db, "evt-multi-consumer-001", "consumer-A")
        record_b = await _get_decision(db, "evt-multi-consumer-001", "consumer-B")
        assert record_a is not None and record_b is not None
        assert record_a.result == "A-result"
        assert record_b.result == "B-result"

    @pytest.mark.asyncio
    async def test_different_content_for_different_consumers_allowed(
        self, db_with_decisions: Database
    ) -> None:
        """不同 consumer 对同 event_id 用不同内容不冲突（主键含 consumer_id）。"""
        db = db_with_decisions
        event_a = _make_event("evt-diff-consumer-001", payload={"who": "A"})
        event_b = _make_event("evt-diff-consumer-001", payload={"who": "B"})

        await _record(
            db,
            "evt-diff-consumer-001",
            "consumer-A",
            EVENT_DECISION_APPLIED,
            content_hash=compute_event_content_hash(event_a),
        )
        # consumer-B 用不同内容记录不抛冲突
        await _record(
            db,
            "evt-diff-consumer-001",
            "consumer-B",
            EVENT_DECISION_APPLIED,
            content_hash=compute_event_content_hash(event_b),
        )
        assert await _has_processed(db, "evt-diff-consumer-001", "consumer-A") is True
        assert await _has_processed(db, "evt-diff-consumer-001", "consumer-B") is True


# --------------------------------------------------------------------------- #
# 验收 5：FAILED 决策可重试
# --------------------------------------------------------------------------- #


class TestFailedRetryable:
    """failed 判定可被后续 record_decision 覆盖；has_processed 对 failed 返回 False。"""

    @pytest.mark.asyncio
    async def test_failed_not_considered_processed(self, db_with_decisions: Database) -> None:
        db = db_with_decisions
        event = _make_event("evt-failed-001")
        await _record(
            db,
            "evt-failed-001",
            "consumer-A",
            EVENT_DECISION_FAILED,
            error="boom",
            content_hash=compute_event_content_hash(event),
        )
        assert await _has_processed(db, "evt-failed-001", "consumer-A") is False

    @pytest.mark.asyncio
    async def test_failed_overwritten_by_applied(self, db_with_decisions: Database) -> None:
        """failed 后重试 applied：覆盖更新，version_no 递增。"""
        db = db_with_decisions
        event = _make_event("evt-failed-002")
        content_hash = compute_event_content_hash(event)

        await _record(
            db,
            "evt-failed-002",
            "consumer-A",
            EVENT_DECISION_FAILED,
            error="first-attempt-failed",
            content_hash=content_hash,
        )
        # 重试：同内容，原判定 failed → 覆盖为 applied
        await _record(
            db,
            "evt-failed-002",
            "consumer-A",
            EVENT_DECISION_APPLIED,
            result="ok",
            content_hash=content_hash,
        )

        record = await _get_decision(db, "evt-failed-002", "consumer-A")
        assert record is not None
        assert record.decision == EVENT_DECISION_APPLIED
        assert record.result == "ok"
        assert record.error is None
        assert record.version_no == 2
        assert await _has_processed(db, "evt-failed-002", "consumer-A") is True

    @pytest.mark.asyncio
    async def test_failed_then_failed_increments_version(
        self, db_with_decisions: Database
    ) -> None:
        """多次 failed 都可覆盖，version_no 每次递增。"""
        db = db_with_decisions
        event = _make_event("evt-failed-003")
        content_hash = compute_event_content_hash(event)

        await _record(
            db, "evt-failed-003", "consumer-A", EVENT_DECISION_FAILED,
            error="e1", content_hash=content_hash,
        )
        await _record(
            db, "evt-failed-003", "consumer-A", EVENT_DECISION_FAILED,
            error="e2", content_hash=content_hash,
        )
        record = await _get_decision(db, "evt-failed-003", "consumer-A")
        assert record is not None
        assert record.decision == EVENT_DECISION_FAILED
        assert record.error == "e2"
        assert record.version_no == 2
        assert await _has_processed(db, "evt-failed-003", "consumer-A") is False


# --------------------------------------------------------------------------- #
# 验收 6：事件内容不被修改
# --------------------------------------------------------------------------- #


class TestEventContentUnchanged:
    """判定记录流程不修改事件 dict 本身（事件是 Git coordination 事实源）。"""

    @pytest.mark.asyncio
    async def test_record_decision_does_not_mutate_event(
        self, db_with_decisions: Database
    ) -> None:
        db = db_with_decisions
        event = _make_event("evt-immutable-001", payload={"k": "v"})
        snapshot_before = copy.deepcopy(event)
        content_hash = compute_event_content_hash(event)

        async with SqliteUnitOfWork(db) as uow:
            repo = EventDecisionRepository(uow.connection)
            # 计算哈希不应修改 event
            assert compute_event_content_hash(event) == content_hash
            await repo.record_decision(
                "evt-immutable-001", "consumer-A", EVENT_DECISION_APPLIED,
                result="ok", error=None, content_hash=content_hash,
            )
            await uow.commit()

        # event dict 未被修改
        assert event == snapshot_before
        # 哈希稳定
        assert compute_event_content_hash(event) == content_hash

    @pytest.mark.asyncio
    async def test_process_idempotently_does_not_mutate_event(
        self, db_with_decisions: Database
    ) -> None:
        db = db_with_decisions
        event = _make_event("evt-immutable-002", payload={"k": "v"})
        snapshot_before = copy.deepcopy(event)

        async def handler(ev: dict) -> str:
            return "ok"

        async with SqliteUnitOfWork(db) as uow:
            repo = EventDecisionRepository(uow.connection)
            await process_event_idempotently(
                event, consumer_id="consumer-A", repository=repo, handler=handler
            )
            await uow.commit()

        assert event == snapshot_before

    @pytest.mark.asyncio
    async def test_skipped_invalid_decision_recorded(
        self, db_with_decisions: Database
    ) -> None:
        """skipped_invalid 判定可记录且视为已处理（非 failed）。"""
        db = db_with_decisions
        event = _make_event("evt-skipped-001")
        await _record(
            db,
            "evt-skipped-001",
            "consumer-A",
            EVENT_DECISION_SKIPPED_INVALID,
            result="invalid assignment_epoch",
            content_hash=compute_event_content_hash(event),
        )
        # skipped_invalid 非 failed → has_processed 返回 True
        assert await _has_processed(db, "evt-skipped-001", "consumer-A") is True
        record = await _get_decision(db, "evt-skipped-001", "consumer-A")
        assert record is not None
        assert record.decision == EVENT_DECISION_SKIPPED_INVALID
        assert record.is_processed is True


# --------------------------------------------------------------------------- #
# 事务原子性：rollback 不写入判定
# --------------------------------------------------------------------------- #


class TestTransactionAtomicity:
    """判定记录随 UnitOfWork 事务提交/回滚。"""

    @pytest.mark.asyncio
    async def test_rollback_rolls_back_decision(self, db_with_decisions: Database) -> None:
        db = db_with_decisions
        event = _make_event("evt-rollback-001")
        content_hash = compute_event_content_hash(event)

        with pytest.raises(RuntimeError, match="boom"):
            async with SqliteUnitOfWork(db) as uow:
                repo = EventDecisionRepository(uow.connection)
                await repo.record_decision(
                    "evt-rollback-001", "consumer-A", EVENT_DECISION_APPLIED,
                    result="ok", error=None, content_hash=content_hash,
                )
                raise RuntimeError("boom")

        # 回滚后无记录
        assert await _has_processed(db, "evt-rollback-001", "consumer-A") is False
        assert await _get_decision(db, "evt-rollback-001", "consumer-A") is None

    @pytest.mark.asyncio
    async def test_commit_persists_decision(self, db_with_decisions: Database) -> None:
        db = db_with_decisions
        event = _make_event("evt-commit-001")
        content_hash = compute_event_content_hash(event)

        async with SqliteUnitOfWork(db) as uow:
            repo = EventDecisionRepository(uow.connection)
            await repo.record_decision(
                "evt-commit-001", "consumer-A", EVENT_DECISION_APPLIED,
                result="ok", error=None, content_hash=content_hash,
            )
            await uow.commit()

        assert await _has_processed(db, "evt-commit-001", "consumer-A") is True
