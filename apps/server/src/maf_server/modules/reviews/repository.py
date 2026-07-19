"""Review 和 Gate Decision 持久化接口。

TASK-080 增量（确定性 Validator 框架配套）：
- ``ARTIFACT_REVIEWS_DDL``：``artifact_reviews`` 表 DDL（id、artifact_id、
  status、validator_results TEXT JSON、reviewer、reviewed_at、version_no）。
- ``init_artifact_reviews_schema``：幂等建表函数（供测试与首次启动使用）。
- ``SqliteArtifactReviewRepository``：``artifact_reviews`` 表 CRUD，提供
  insert/get/list 方法，接受 ``aiosqlite.Connection``，不自开事务。
- ``ArtifactReviewRecord`` dataclass：行映射，供 service 层使用。

TASK-081 增量（Review 与 QualityGate 核心实现）：
- 增强 ``artifact_reviews`` 表：新增 ``review_status``（PENDING/APPROVED/
  REJECTED/CHANGES_REQUESTED）、``reviewer_comment``、``decided_by``、
  ``decided_at`` 字段，记录人工评审决策。
- ``SqliteArtifactReviewRepository`` 新增 ``update_review_status`` 方法
  （approve/reject/request_changes 状态转换，乐观锁）与 ``list_by_artifact``
  的 ``review_status`` 过滤。
- ``QUALITY_GATES_DDL``：``quality_gates`` 表 DDL（id、run_id、node_id、
  gate_definitions TEXT JSON、created_by、created_at、version_no）。
- ``SqliteQualityGateRepository``：``quality_gates`` 表 CRUD，提供
  insert/get 方法。
- ``QualityGateRecord`` dataclass：行映射。

事务边界：repository 方法接受 ``aiosqlite.Connection``（由 ``SqliteUnitOfWork``
提供），不自开事务；service 层负责 ``BEGIN IMMEDIATE``/``COMMIT``。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, cast

import aiosqlite

from .schemas import (
    ArtifactReviewStatus,
    ArtifactReviewView,
    GateDefinition,
    GateDecisionView,
    QualityGateConfig,
    ReviewPage,
    ReviewQuery,
    ReviewStatus,
    ReviewView,
)

# --------------------------------------------------------------------------- #
# 表结构 DDL（供测试与首次启动建表使用；正式部署由 migrations 负责）
# --------------------------------------------------------------------------- #

ARTIFACT_REVIEWS_DDL: str = """
CREATE TABLE IF NOT EXISTS artifact_reviews (
    id                TEXT    PRIMARY KEY,
    artifact_id       TEXT    NOT NULL,
    status            TEXT    NOT NULL,
    validator_results TEXT    NOT NULL CHECK(json_valid(validator_results)),
    reviewer          TEXT    NOT NULL,
    reviewed_at       TEXT    NOT NULL,
    version_no        INTEGER NOT NULL DEFAULT 1,
    review_status     TEXT    NOT NULL DEFAULT 'PENDING',
    reviewer_comment  TEXT,
    decided_by        TEXT,
    decided_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_artifact_reviews_artifact
    ON artifact_reviews(artifact_id);
CREATE INDEX IF NOT EXISTS idx_artifact_reviews_status
    ON artifact_reviews(status);
CREATE INDEX IF NOT EXISTS idx_artifact_reviews_review_status
    ON artifact_reviews(review_status);
"""

QUALITY_GATES_DDL: str = """
CREATE TABLE IF NOT EXISTS quality_gates (
    id                TEXT    PRIMARY KEY,
    run_id            TEXT    NOT NULL,
    node_id           TEXT,
    gate_definitions  TEXT    NOT NULL CHECK(json_valid(gate_definitions)),
    created_by        TEXT    NOT NULL,
    created_at        TEXT    NOT NULL,
    version_no        INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_quality_gates_run
    ON quality_gates(run_id);
CREATE INDEX IF NOT EXISTS idx_quality_gates_run_node
    ON quality_gates(run_id, node_id);
"""


async def init_artifact_reviews_schema(conn: aiosqlite.Connection) -> None:
    """在给定连接上创建 ``artifact_reviews`` 表与索引（``CREATE TABLE IF NOT
    EXISTS``，幂等）。

    正式部署由 ``migrations/`` 顺序迁移负责；本函数供测试与开发期首次启动使用。

    实现说明：使用逐条 ``execute`` 而非 ``executescript``，因为
    ``executescript`` 会隐式 COMMIT 当前事务，与 ``Database.write_connection``
    的 ``BEGIN IMMEDIATE``/``COMMIT`` 边界冲突。
    """
    for raw in ARTIFACT_REVIEWS_DDL.split(";"):
        stmt = raw.strip()
        if stmt:
            await conn.execute(stmt)


async def init_quality_gates_schema(conn: aiosqlite.Connection) -> None:
    """在给定连接上创建 ``quality_gates`` 表与索引（幂等）。

    正式部署由 ``migrations/`` 顺序迁移负责；本函数供测试与开发期首次启动使用。
    """
    for raw in QUALITY_GATES_DDL.split(";"):
        stmt = raw.strip()
        if stmt:
            await conn.execute(stmt)


def new_review_id() -> str:
    """生成新 Review ID（UUID v4 字符串）。"""
    return str(uuid.uuid4())


def new_gate_config_id() -> str:
    """生成新 QualityGate 配置 ID（UUID v4 字符串）。"""
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- #
# TASK-080 + TASK-081：artifact_reviews 行映射与 SQLite 仓储
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ArtifactReviewRecord:
    """``artifact_reviews`` 表行映射，供 service 层内部使用。"""

    id: str
    artifact_id: str
    status: ArtifactReviewStatus
    validator_results: list[dict[str, Any]]
    reviewer: str
    reviewed_at: str
    version_no: int
    review_status: ReviewStatus
    reviewer_comment: str | None
    decided_by: str | None
    decided_at: str | None


_REVIEW_COLUMNS: str = (
    "id, artifact_id, status, validator_results, reviewer, "
    "reviewed_at, version_no, review_status, reviewer_comment, "
    "decided_by, decided_at"
)


def _row_to_review(row: aiosqlite.Row | tuple) -> ArtifactReviewRecord:
    """把 ``artifact_reviews`` 表行映射为 ``ArtifactReviewRecord``。"""
    if isinstance(row, aiosqlite.Row):
        row = tuple(row)
    raw_results = str(row[3])
    try:
        validator_results = json.loads(raw_results)
    except json.JSONDecodeError:  # 防御：CHECK(json_valid) 已保证，不应触发
        validator_results = []
    if not isinstance(validator_results, list):
        validator_results = []
    review_status = str(row[7]) if row[7] is not None else "PENDING"
    if review_status not in ("PENDING", "APPROVED", "REJECTED", "CHANGES_REQUESTED"):
        review_status = "PENDING"
    return ArtifactReviewRecord(
        id=str(row[0]),
        artifact_id=str(row[1]),
        status=cast(ArtifactReviewStatus, str(row[2])),
        validator_results=validator_results,
        reviewer=str(row[4]),
        reviewed_at=str(row[5]),
        version_no=int(row[6]),
        review_status=cast(ReviewStatus, review_status),
        reviewer_comment=row[8],  # str | None
        decided_by=row[9],  # str | None
        decided_at=row[10],  # str | None
    )


def review_record_to_view(rec: ArtifactReviewRecord) -> ArtifactReviewView:
    """把 ``ArtifactReviewRecord`` 映射为对外 ``ArtifactReviewView``。"""
    return ArtifactReviewView(
        id=rec.id,
        artifact_id=rec.artifact_id,
        status=rec.status,
        validator_results=rec.validator_results,
        reviewer=rec.reviewer,
        reviewed_at=rec.reviewed_at,
        version_no=rec.version_no,
        review_status=rec.review_status,
        reviewer_comment=rec.reviewer_comment,
        decided_by=rec.decided_by,
        decided_at=rec.decided_at,
    )


class SqliteArtifactReviewRepository:
    """``artifact_reviews`` 表的 SQLite 仓储实现。

    所有方法接受 ``aiosqlite.Connection``（由 ``SqliteUnitOfWork`` 提供），
    不自开事务、不调用网络、不写日志。

    谁调用它：
        ``ArtifactReviewServiceImpl`` 在 ``SqliteUnitOfWork`` 事务内调用本类方法
        读写 artifact 评审记录。

    安全约束：
        - ``validator_results`` 以 TEXT 存储，``CHECK(json_valid)`` 在 DB 层保证
          合法 JSON；
        - ``status`` 取值由 service 层通过 ``aggregate_review_status`` 汇总得到，
          本类不重复汇总逻辑；
        - ``review_status`` 状态转换由 service 层校验，本类只负责持久化；
        - 乐观锁：``update_review_status`` 通过 ``version_no`` 防止并发覆盖。
    """

    async def insert_review(
        self,
        conn: aiosqlite.Connection,
        *,
        review_id: str,
        artifact_id: str,
        status: ArtifactReviewStatus,
        validator_results: list[dict[str, Any]],
        reviewer: str,
        reviewed_at: str,
        review_status: ReviewStatus = "PENDING",
        reviewer_comment: str | None = None,
        decided_by: str | None = None,
        decided_at: str | None = None,
    ) -> None:
        """插入一行 artifact 评审记录。

        ``validator_results`` 会被序列化为 JSON 字符串存储。调用方应保证
        ``status`` 与 ``validator_results`` 一致（通过
        ``aggregate_review_status`` 汇总）。

        TASK-081：``review_status`` 默认 PENDING，``decided_by``/``decided_at``
        默认 None（submit 时由 service 层填充为 reviewer/reviewed_at）。
        """
        await conn.execute(
            "INSERT INTO artifact_reviews "
            "(id, artifact_id, status, validator_results, reviewer, "
            "reviewed_at, version_no, review_status, reviewer_comment, "
            "decided_by, decided_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (
                review_id,
                artifact_id,
                status,
                json.dumps(validator_results, ensure_ascii=False),
                reviewer,
                reviewed_at,
                review_status,
                reviewer_comment,
                decided_by,
                decided_at,
            ),
        )

    async def update_review_status(
        self,
        conn: aiosqlite.Connection,
        review_id: str,
        *,
        new_review_status: ReviewStatus,
        reviewer_comment: str | None,
        decided_by: str,
        decided_at: str,
        expected_version: int,
    ) -> int:
        """更新评审工作流状态（乐观锁）。

        :returns: 新版本号（>0）表示更新成功；0 表示版本冲突或记录不存在。
        """
        cur = await conn.execute(
            "UPDATE artifact_reviews "
            "SET review_status = ?, reviewer_comment = ?, "
            "decided_by = ?, decided_at = ?, version_no = version_no + 1 "
            "WHERE id = ? AND version_no = ?",
            (
                new_review_status,
                reviewer_comment,
                decided_by,
                decided_at,
                review_id,
                expected_version,
            ),
        )
        new_version = cur.rowcount if cur.rowcount > 0 else 0
        await cur.close()
        return new_version

    async def get_review(
        self,
        conn: aiosqlite.Connection,
        review_id: str,
    ) -> ArtifactReviewRecord | None:
        """按 id 查询评审记录；不存在返回 None。"""
        sql = (
            f"SELECT {_REVIEW_COLUMNS} FROM artifact_reviews "
            "WHERE id = ? LIMIT 1"
        )
        async with conn.execute(sql, (review_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_review(row) if row is not None else None

    async def list_by_artifact(
        self,
        conn: aiosqlite.Connection,
        artifact_id: str,
        *,
        review_status: ReviewStatus | None = None,
        limit: int = 100,
    ) -> list[ArtifactReviewRecord]:
        """按 artifact_id 列出评审记录，按 ``reviewed_at`` 降序（最新在前）。

        TASK-081：``review_status`` 过滤可选，为 None 时返回全部状态。

        默认上限 100，最大 500。
        """
        effective_limit = max(1, min(limit, 500))
        if review_status is not None:
            sql = (
                f"SELECT {_REVIEW_COLUMNS} FROM artifact_reviews "
                "WHERE artifact_id = ? AND review_status = ? "
                "ORDER BY reviewed_at DESC, id ASC LIMIT ?"
            )
            params: tuple[Any, ...] = (artifact_id, review_status, effective_limit)
        else:
            sql = (
                f"SELECT {_REVIEW_COLUMNS} FROM artifact_reviews "
                "WHERE artifact_id = ? "
                "ORDER BY reviewed_at DESC, id ASC LIMIT ?"
            )
            params = (artifact_id, effective_limit)
        async with conn.execute(sql, params) as cur:
            rows = list(await cur.fetchall())
        return [_row_to_review(r) for r in rows]


# --------------------------------------------------------------------------- #
# TASK-081：quality_gates 行映射与 SQLite 仓储
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class QualityGateRecord:
    """``quality_gates`` 表行映射，供 service 层内部使用。"""

    id: str
    run_id: str
    node_id: str | None
    gate_definitions: list[dict[str, Any]]
    created_by: str
    created_at: str
    version_no: int


_GATE_COLUMNS: str = (
    "id, run_id, node_id, gate_definitions, created_by, "
    "created_at, version_no"
)


def _row_to_gate_config(row: aiosqlite.Row | tuple) -> QualityGateRecord:
    """把 ``quality_gates`` 表行映射为 ``QualityGateRecord``。"""
    if isinstance(row, aiosqlite.Row):
        row = tuple(row)
    raw_defs = str(row[3])
    try:
        gate_definitions = json.loads(raw_defs)
    except json.JSONDecodeError:  # 防御：CHECK(json_valid) 已保证
        gate_definitions = []
    if not isinstance(gate_definitions, list):
        gate_definitions = []
    return QualityGateRecord(
        id=str(row[0]),
        run_id=str(row[1]),
        node_id=row[2],  # str | None
        gate_definitions=gate_definitions,
        created_by=str(row[4]),
        created_at=str(row[5]),
        version_no=int(row[6]),
    )


def gate_record_to_view(rec: QualityGateRecord) -> QualityGateConfig:
    """把 ``QualityGateRecord`` 映射为对外 ``QualityGateConfig``。"""
    return QualityGateConfig(
        id=rec.id,
        run_id=rec.run_id,
        node_id=rec.node_id,
        gate_definitions=cast(list[GateDefinition], rec.gate_definitions),
        created_by=rec.created_by,
        created_at=rec.created_at,
        version_no=rec.version_no,
    )


class SqliteQualityGateRepository:
    """``quality_gates`` 表的 SQLite 仓储实现。

    所有方法接受 ``aiosqlite.Connection``（由 ``SqliteUnitOfWork`` 提供），
    不自开事务、不调用网络、不写日志。

    谁调用它：
        ``QualityGateServiceImpl`` 在 ``SqliteUnitOfWork`` 事务内调用本类方法
        读写 quality_gates 配置。

    安全约束：
        - ``gate_definitions`` 以 TEXT 存储，``CHECK(json_valid)`` 保证合法 JSON；
        - ``(run_id, node_id)`` 唯一约束通过 ``upsert`` 实现（set 时覆盖旧配置）；
        - ``node_id`` 为 NULL 表示 Run 级别门禁。
    """

    async def upsert_gate(
        self,
        conn: aiosqlite.Connection,
        *,
        config_id: str,
        run_id: str,
        node_id: str | None,
        gate_definitions: list[dict[str, Any]],
        created_by: str,
        created_at: str,
    ) -> str:
        """插入或覆盖 (run_id, node_id) 的 quality_gate 配置。

        若 (run_id, node_id) 已存在，删除旧行后插入新行（保持 config_id 更新，
        version_no 从 1 开始）。返回 config_id。
        """
        # 先删除旧配置（幂等）
        await conn.execute(
            "DELETE FROM quality_gates WHERE run_id = ? AND "
            "(node_id IS ? OR (node_id = ? AND ? IS NOT NULL))",
            (run_id, node_id, node_id, node_id),
        )
        await conn.execute(
            "INSERT INTO quality_gates "
            "(id, run_id, node_id, gate_definitions, created_by, "
            "created_at, version_no) VALUES (?, ?, ?, ?, ?, ?, 1)",
            (
                config_id,
                run_id,
                node_id,
                json.dumps(gate_definitions, ensure_ascii=False),
                created_by,
                created_at,
            ),
        )
        return config_id

    async def get_gate(
        self,
        conn: aiosqlite.Connection,
        run_id: str,
        node_id: str | None,
    ) -> QualityGateRecord | None:
        """按 (run_id, node_id) 查询 quality_gate 配置；不存在返回 None。"""
        sql = (
            f"SELECT {_GATE_COLUMNS} FROM quality_gates "
            "WHERE run_id = ? AND node_id IS ? LIMIT 1"
        )
        async with conn.execute(sql, (run_id, node_id)) as cur:
            row = await cur.fetchone()
        return _row_to_gate_config(row) if row is not None else None


# --------------------------------------------------------------------------- #
# TASK-081（保留）：通用 ReviewRepository Protocol
# --------------------------------------------------------------------------- #


class ReviewRepository(Protocol):
    async def list_reviews(self, query: "ReviewQuery", visible_project_ids: set[str]) -> "ReviewPage":
        """在可见项目内按 run/type/status 过滤并游标分页。"""
        ...

    async def get_many(self, review_ids: list[str]) -> list["ReviewView"]:
        """批量读取并保持输入 ID 顺序；缺失项不静默忽略，应由 Service 判为 Gate 材料缺失。"""
        ...

    async def save(self, review: "ReviewView") -> "ReviewView":
        """保存一次评审事实和证据引用；完成后正文不可覆盖。"""
        ...

    async def save_gate_decision(self, decision: "GateDecisionView") -> "GateDecisionView":
        """按 run+gate+输入版本哈希幂等保存确定性决策。"""
        ...


__all__ = [
    "ARTIFACT_REVIEWS_DDL",
    "ArtifactReviewRecord",
    "QualityGateConfig",
    "QualityGateRecord",
    "QUALITY_GATES_DDL",
    "ReviewRepository",
    "SqliteArtifactReviewRepository",
    "SqliteQualityGateRepository",
    "gate_record_to_view",
    "init_artifact_reviews_schema",
    "init_quality_gates_schema",
    "new_gate_config_id",
    "new_review_id",
    "review_record_to_view",
]
