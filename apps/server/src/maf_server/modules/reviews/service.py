"""Review 查询与确定性 Quality Gate 接口。

TASK-080 增量（确定性 Validator 框架配套）：
- ``ArtifactReviewServiceImpl``：``artifact_reviews`` 表的 service 实现，
  提供 ``submit_review`` / ``get_review`` / ``list_reviews`` 三个方法。
  - ``submit_review``：接收 ``artifact_id`` 与 ``ValidatorResult`` 列表，
    通过 ``aggregate_review_status`` 汇总整体状态后写入 ``artifact_reviews`` 表。
    ERROR 视为失败（不能降级为 PASS）。
  - ``get_review`` / ``list_reviews``：从 ``artifact_reviews`` 表读取评审记录。
  - 权限检查：通过 ``PermissionService.require`` 校验 ``reviews`` 资源
    （与 DEFAULT_POLICIES 中 ``("APPROVER", "reviews", ".*")`` 对齐）。

TASK-081 增量（Review 与 QualityGate 核心实现）：
- ``ArtifactReviewServiceImpl`` 增强：
  - ``submit_review`` 新增 ``comment`` 参数，``review_status`` 初始为 PENDING；
  - ``list_reviews`` 新增 ``status`` 参数（按 review_status 过滤）；
  - ``approve_review`` / ``reject_review`` / ``request_changes``：人工评审决策，
    状态流转 PENDING→APPROVED/REJECTED/CHANGES_REQUESTED（CHANGES_REQUESTED
    可再次 approve/reject），需 ``write reviews`` 权限（APPROVER/ADMIN）；
  - 事件：``review.approved`` / ``review.rejected`` / ``review.changes_requested``
    经 ``SqliteEventPublisher`` 写入 Outbox。
- ``QualityGateServiceImpl``：确定性 Quality Gate 评估。
  - ``evaluate``：按 ``GateDefinition`` 列表评估 artifact 最新评审的 Validator
    结果，blocking 门禁失败时整体不通过（阻断项不能被忽略）；
  - ``get_quality_gate`` / ``set_quality_gate``：读写 ``quality_gates`` 表；
  - 事件：``quality_gate.evaluated`` 经 Outbox 写入。
- ``ReviewService`` / ``QualityGateService`` Protocol 更新为本任务接口。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from maf_contracts.common import ActorContext
from maf_domain.errors import (
    ArgumentError,
    NotFoundError,
    PermissionDeniedError,
    UnauthenticatedError,
    UnsupportedOperationError,
    VersionConflictError,
)
from maf_policy import CasbinPermissionService
from maf_server.core.database import Database
from maf_server.core.events import SqliteEventPublisher
from maf_server.core.unit_of_work import SqliteUnitOfWork
from maf_contracts.events import ActorRef, DomainEvent

from ..artifacts.service import (
    RESOURCE_REVIEWS,
    ValidatorResult,
    aggregate_review_status,
)
from .repository import (
    ArtifactReviewRecord,
    QualityGateRecord,
    SqliteArtifactReviewRepository,
    SqliteQualityGateRepository,
    gate_record_to_view,
    new_gate_config_id,
    new_review_id,
    review_record_to_view,
)
from .schemas import (
    ArtifactReviewStatus,
    ArtifactReviewView,
    GateDefinition,
    GateResult,
    QualityGateConfig,
    QualityGateResult,
    ReviewStatus,
    ReviewPage,
    ReviewQuery,
    ReviewView,
)

# --------------------------------------------------------------------------- #
# 资源与动作常量
# --------------------------------------------------------------------------- #

ACTION_READ: str = "read"
ACTION_WRITE: str = "write"

#: 质量门禁资源（与 DEFAULT_POLICIES 中 reviews 策略共用，APPROVER 可读写）。
RESOURCE_QUALITY_GATES: str = "reviews"

# --------------------------------------------------------------------------- #
# 内部时钟（与 artifacts.service._SystemClock 对齐，避免跨模块导入私有类）
# --------------------------------------------------------------------------- #


class _SystemClock:
    """默认使用系统 UTC 时钟；测试可注入虚拟时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #


def _ensure_iso(value: datetime) -> str:
    """把 datetime 序列化为带时区 ISO 8601 字符串。naive 视为 UTC。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _require_actor(actor: ActorContext) -> str:
    """校验 actor 并返回 user_id。

    未认证抛 ``UnauthenticatedError``。组织 ID 缺失时回退为 ``"system"``。
    """
    if not isinstance(actor, dict):
        raise UnauthenticatedError("未认证")
    user_id = actor.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        raise UnauthenticatedError("未认证")
    return user_id


def _actor_context(
    actor_id: str, actor: ActorContext | None
) -> tuple[ActorContext, str, str, str]:
    """构造/补全 actor 上下文，返回 (ctx, user_id, org_id, trace_id)。"""
    actor_ctx = actor or {
        "user_id": actor_id,
        "organization_id": "system",
        "permission_keys": [],
        "trace_id": "",
    }
    user_id = _require_actor(actor_ctx)
    org_id = actor_ctx.get("organization_id") if isinstance(actor_ctx, dict) else "system"
    if not isinstance(org_id, str) or not org_id:
        org_id = "system"
    trace_id = actor_ctx.get("trace_id") if isinstance(actor_ctx, dict) else ""
    if not isinstance(trace_id, str):
        trace_id = ""
    return actor_ctx, user_id, org_id, trace_id


def _validate_review_status_transition(
    current: ReviewStatus, target: ReviewStatus
) -> None:
    """校验 review_status 状态流转合法性。

    合法流转：
        - PENDING → APPROVED / REJECTED / CHANGES_REQUESTED
        - CHANGES_REQUESTED → APPROVED / REJECTED

    非法流转（抛 ``UnsupportedOperationError``）：
        - APPROVED / REJECTED → 任何状态（终态，不可再决策）
        - PENDING → PENDING（无意义）
        - CHANGES_REQUESTED → CHANGES_REQUESTED（需先重新 submit）
    """
    terminal_states = {"APPROVED", "REJECTED"}
    if current in terminal_states:
        raise UnsupportedOperationError(
            f"评审已处于终态 {current!r}，不可再决策",
            context={"current_status": current, "target_status": target},
        )
    if current == "PENDING":
        if target not in ("APPROVED", "REJECTED", "CHANGES_REQUESTED"):
            raise UnsupportedOperationError(
                f"PENDING 状态只能转为 APPROVED/REJECTED/CHANGES_REQUESTED，"
                f"不能转为 {target!r}",
                context={"current_status": current, "target_status": target},
            )
        return
    if current == "CHANGES_REQUESTED":
        if target not in ("APPROVED", "REJECTED"):
            raise UnsupportedOperationError(
                f"CHANGES_REQUESTED 状态只能转为 APPROVED/REJECTED，"
                f"不能转为 {target!r}",
                context={"current_status": current, "target_status": target},
            )
        return
    raise UnsupportedOperationError(
        f"未知的当前状态 {current!r}",
        context={"current_status": current, "target_status": target},
    )


# --------------------------------------------------------------------------- #
# TASK-080 + TASK-081：ArtifactReviewServiceImpl 具体实现
# --------------------------------------------------------------------------- #


class ArtifactReviewServiceImpl:
    """``artifact_reviews`` 表的 service 实现（TASK-080 + TASK-081）。

    依赖注入：
        - ``database``：``Database``，提供 ``SqliteUnitOfWork`` 事务边界；
        - ``repository``：``SqliteArtifactReviewRepository``，评审记录 CRUD；
        - ``permission_service``：``PermissionService``，默认 CasbinPermissionService；
        - ``clock``：可注入虚拟时钟用于测试。

    权限检查（对应任务目标 4）：
        - 读操作（get_review/list_reviews）：``require(actor, "read", "reviews")``；
        - 写操作（submit_review/approve/reject/request_changes）：
          ``require(actor, "write", "reviews")``。

    ERROR ≠ PASS（验收标准 1）：
        - ``submit_review`` 通过 ``aggregate_review_status`` 汇总 ValidatorResult
          列表：任一 ``ERROR`` → 整体 ``ERROR``；任一 ``FAIL`` → 整体 ``FAIL``；
          否则 ``PASS``。``ERROR`` 状态的评审记录被视为失败，不会降级为 ``PASS``。

    状态流转（对应任务目标 4：ReviewStatus 状态流转正确）：
        - ``submit_review`` → review_status=PENDING；
        - ``approve_review`` → PENDING/CHANGES_REQUESTED → APPROVED；
        - ``reject_review`` → PENDING/CHANGES_REQUESTED → REJECTED；
        - ``request_changes`` → PENDING → CHANGES_REQUESTED。
        APPROVED/REJECTED 为终态，不可再决策。

    事务边界：
        - ``submit_review``/``approve``/``reject``/``request_changes``：
          UoW 内写入 + 事件 commit；
        - ``get_review``/``list_reviews``：UoW 内只读（rollback）。
    """

    def __init__(
        self,
        database: Database,
        *,
        repository: SqliteArtifactReviewRepository | None = None,
        permission_service: "PermissionService | None" = None,
        clock: _SystemClock | None = None,
    ) -> None:
        self._database: Database = database
        self._repository: SqliteArtifactReviewRepository = (
            repository or SqliteArtifactReviewRepository()
        )
        self._permission_service: "PermissionService" = (
            permission_service or CasbinPermissionService()
        )
        self._clock: _SystemClock = clock or _SystemClock()

    # ------------------------------------------------------------------ #
    # submit_review
    # ------------------------------------------------------------------ #

    async def submit_review(
        self,
        artifact_id: str,
        validator_results: list[ValidatorResult],
        *,
        actor_id: str,
        actor: ActorContext | None = None,
        comment: str | None = None,
    ) -> ArtifactReviewView:
        """提交 artifact 的 Validator 校验结果，写入 ``artifact_reviews`` 表。

        TASK-081：新增 ``comment`` 参数（人工评论，可空）。提交时
        ``review_status`` 初始为 ``PENDING``，``decided_by``/``decided_at``
        填充为提交者与提交时间。

        实现顺序：
            1. 校验 actor 与权限（write reviews）；
            2. 校验 artifact_id 非空、validator_results 是 list、comment 是 str|None；
            3. 通过 ``aggregate_review_status`` 汇总整体状态（ERROR ≠ PASS）；
            4. UoW 内：INSERT 评审记录 + Outbox 事件 + commit；
            5. 返回 ``ArtifactReviewView``。

        :raises PermissionDeniedError: 无 write reviews 权限。
        :raises ArgumentError: 参数非法。
        """
        actor_ctx, actor_user_id, org_id, trace_id = _actor_context(actor_id, actor)
        await self._permission_service.require(
            actor_ctx, ACTION_WRITE, RESOURCE_REVIEWS
        )

        if not isinstance(artifact_id, str) or not artifact_id.strip():
            raise ArgumentError("artifact_id 不能为空")
        if not isinstance(validator_results, list):
            raise ArgumentError("validator_results 必须是 list")
        if comment is not None and not isinstance(comment, str):
            raise ArgumentError("comment 必须是 str 或 None")

        # 汇总整体状态（ERROR ≠ PASS：任一 ERROR → ERROR；任一 FAIL → FAIL）
        status: ArtifactReviewStatus = aggregate_review_status(validator_results)
        # 序列化 validator_results 为 JSON 兼容 list[dict]
        serialized_results: list[dict[str, Any]] = [
            r.to_dict() if isinstance(r, ValidatorResult) else dict(r)
            for r in validator_results
        ]

        now = self._clock.now()
        iso = _ensure_iso(now)
        review_id = new_review_id()
        artifact_id_clean = artifact_id.strip()
        comment_clean = comment.strip() if comment else None

        async with SqliteUnitOfWork(self._database) as uow:
            await self._repository.insert_review(
                uow.connection,
                review_id=review_id,
                artifact_id=artifact_id_clean,
                status=status,
                validator_results=serialized_results,
                reviewer=actor_user_id,
                reviewed_at=iso,
                review_status="PENDING",
                reviewer_comment=comment_clean,
                decided_by=actor_user_id,
                decided_at=iso,
            )
            await self._append_event(
                uow.connection,
                event_type="artifact.review_submitted",
                aggregate_id=review_id,
                artifact_id=artifact_id_clean,
                actor_id=actor_user_id,
                org_id=org_id,
                trace_id=trace_id,
                payload={
                    "artifact_id": artifact_id_clean,
                    "status": status,
                    "review_status": "PENDING",
                    "validator_count": len(serialized_results),
                    "validator_names": [
                        r.get("validator_name", "") for r in serialized_results
                    ],
                },
            )
            await uow.commit()

        rec = await self._load_review(review_id)
        assert rec is not None  # 刚写入，必然存在
        return review_record_to_view(rec)

    # ------------------------------------------------------------------ #
    # get_review
    # ------------------------------------------------------------------ #

    async def get_review(
        self,
        review_id: str,
        *,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> ArtifactReviewView:
        """按 review_id 获取评审记录。

        :raises NotFoundError: 评审记录不存在。
        :raises PermissionDeniedError: 无 read reviews 权限。
        """
        actor_ctx, _, _, _ = _actor_context(actor_id, actor)
        await self._permission_service.require(
            actor_ctx, ACTION_READ, RESOURCE_REVIEWS
        )

        if not isinstance(review_id, str) or not review_id.strip():
            raise ArgumentError("review_id 不能为空")

        rec = await self._load_review(review_id)
        if rec is None:
            raise NotFoundError(
                "评审记录不存在", context={"review_id": review_id}
            )
        return review_record_to_view(rec)

    # ------------------------------------------------------------------ #
    # list_reviews
    # ------------------------------------------------------------------ #

    async def list_reviews(
        self,
        artifact_id: str,
        *,
        actor_id: str,
        actor: ActorContext | None = None,
        status: ReviewStatus | None = None,
    ) -> list[ArtifactReviewView]:
        """按 artifact_id 列出评审记录，按 ``reviewed_at`` 降序（最新在前）。

        TASK-081：新增 ``status`` 参数（按 ``review_status`` 过滤，为 None 时
        返回全部）。

        :raises PermissionDeniedError: 无 read reviews 权限。
        """
        actor_ctx, _, _, _ = _actor_context(actor_id, actor)
        await self._permission_service.require(
            actor_ctx, ACTION_READ, RESOURCE_REVIEWS
        )

        if not isinstance(artifact_id, str) or not artifact_id.strip():
            raise ArgumentError("artifact_id 不能为空")

        if status is not None and status not in (
            "PENDING",
            "APPROVED",
            "REJECTED",
            "CHANGES_REQUESTED",
        ):
            raise ArgumentError(
                f"status 必须是 PENDING/APPROVED/REJECTED/CHANGES_REQUESTED: "
                f"{status!r}"
            )

        async with SqliteUnitOfWork(self._database) as uow:
            recs = await self._repository.list_by_artifact(
                uow.connection, artifact_id, review_status=status
            )
            await uow.rollback()
        return [review_record_to_view(r) for r in recs]

    # ------------------------------------------------------------------ #
    # approve_review / reject_review / request_changes（TASK-081）
    # ------------------------------------------------------------------ #

    async def approve_review(
        self,
        review_id: str,
        *,
        actor_id: str,
        comment: str,
        actor: ActorContext | None = None,
    ) -> ArtifactReviewView:
        """人工批准评审（PENDING/CHANGES_REQUESTED → APPROVED）。

        :raises PermissionDeniedError: 无 write reviews 权限（需 APPROVER/ADMIN）。
        :raises NotFoundError: 评审记录不存在。
        :raises UnsupportedOperationError: 当前状态不允许 approve（终态）。
        :raises ArgumentError: comment 为空。
        """
        return await self._transition_review_status(
            review_id,
            target_status="APPROVED",
            event_type="review.approved",
            actor_id=actor_id,
            comment=comment,
            actor=actor,
        )

    async def reject_review(
        self,
        review_id: str,
        *,
        actor_id: str,
        comment: str,
        actor: ActorContext | None = None,
    ) -> ArtifactReviewView:
        """人工拒绝评审（PENDING/CHANGES_REQUESTED → REJECTED）。

        :raises PermissionDeniedError: 无 write reviews 权限（需 APPROVER/ADMIN）。
        :raises NotFoundError: 评审记录不存在。
        :raises UnsupportedOperationError: 当前状态不允许 reject（终态）。
        :raises ArgumentError: comment 为空。
        """
        return await self._transition_review_status(
            review_id,
            target_status="REJECTED",
            event_type="review.rejected",
            actor_id=actor_id,
            comment=comment,
            actor=actor,
        )

    async def request_changes(
        self,
        review_id: str,
        *,
        actor_id: str,
        comment: str,
        actor: ActorContext | None = None,
    ) -> ArtifactReviewView:
        """请求修改（PENDING → CHANGES_REQUESTED）。

        :raises PermissionDeniedError: 无 write reviews 权限（需 APPROVER/ADMIN）。
        :raises NotFoundError: 评审记录不存在。
        :raises UnsupportedOperationError: 当前状态不允许 request_changes。
        :raises ArgumentError: comment 为空。
        """
        return await self._transition_review_status(
            review_id,
            target_status="CHANGES_REQUESTED",
            event_type="review.changes_requested",
            actor_id=actor_id,
            comment=comment,
            actor=actor,
        )

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    async def _transition_review_status(
        self,
        review_id: str,
        *,
        target_status: ReviewStatus,
        event_type: str,
        actor_id: str,
        comment: str,
        actor: ActorContext | None,
    ) -> ArtifactReviewView:
        """通用状态转换逻辑（approve/reject/request_changes 共用）。

        实现顺序：
            1. 校验 actor 与权限（write reviews）；
            2. 校验 comment 非空（人工决策必须说明理由）；
            3. UoW 内：读评审记录 → 校验状态流转 → 乐观锁更新 → 事件 → commit；
            4. 返回更新后的 ``ArtifactReviewView``。
        """
        actor_ctx, actor_user_id, org_id, trace_id = _actor_context(actor_id, actor)
        await self._permission_service.require(
            actor_ctx, ACTION_WRITE, RESOURCE_REVIEWS
        )

        if not isinstance(review_id, str) or not review_id.strip():
            raise ArgumentError("review_id 不能为空")
        if not isinstance(comment, str) or not comment.strip():
            raise ArgumentError("comment 不能为空（人工决策必须说明理由）")

        now = self._clock.now()
        iso = _ensure_iso(now)
        comment_clean = comment.strip()

        async with SqliteUnitOfWork(self._database) as uow:
            rec = await self._repository.get_review(
                uow.connection, review_id
            )
            if rec is None:
                await uow.rollback()
                raise NotFoundError(
                    "评审记录不存在", context={"review_id": review_id}
                )

            _validate_review_status_transition(rec.review_status, target_status)

            new_version = await self._repository.update_review_status(
                uow.connection,
                review_id,
                new_review_status=target_status,
                reviewer_comment=comment_clean,
                decided_by=actor_user_id,
                decided_at=iso,
                expected_version=rec.version_no,
            )
            if new_version == 0:
                await uow.rollback()
                raise VersionConflictError(
                    "评审记录版本冲突",
                    context={
                        "review_id": review_id,
                        "expected_version": rec.version_no,
                    },
                    retryable=True,
                )

            await self._append_event(
                uow.connection,
                event_type=event_type,
                aggregate_id=review_id,
                artifact_id=rec.artifact_id,
                actor_id=actor_user_id,
                org_id=org_id,
                trace_id=trace_id,
                payload={
                    "artifact_id": rec.artifact_id,
                    "review_id": review_id,
                    "previous_status": rec.review_status,
                    "new_status": target_status,
                    "comment": comment_clean,
                    "decided_by": actor_user_id,
                },
            )
            await uow.commit()

        rec = await self._load_review(review_id)
        assert rec is not None
        return review_record_to_view(rec)

    async def _load_review(self, review_id: str) -> ArtifactReviewRecord | None:
        async with SqliteUnitOfWork(self._database) as uow:
            rec = await self._repository.get_review(uow.connection, review_id)
            await uow.rollback()
        return rec

    async def _append_event(
        self,
        conn,
        *,
        event_type: str,
        aggregate_id: str,
        artifact_id: str,
        actor_id: str,
        org_id: str,
        trace_id: str,
        payload: dict,
    ) -> None:
        """在同一 UoW 事务内向 Outbox 追加评审事件。"""
        publisher = SqliteEventPublisher(conn)
        await publisher.append(
            DomainEvent(
                event_type=event_type,
                aggregate_type="artifact_review",
                aggregate_id=aggregate_id,
                organization_id=org_id,
                project_id=None,
                actor=ActorRef(actor_type="USER", actor_id=actor_id),
                trace_id=trace_id,
                payload=payload,
            )
        )


# --------------------------------------------------------------------------- #
# TASK-081：QualityGateServiceImpl 具体实现
# --------------------------------------------------------------------------- #


class QualityGateServiceImpl:
    """确定性 Quality Gate 评估服务（TASK-081）。

    依赖注入：
        - ``database``：``Database``，提供 ``SqliteUnitOfWork`` 事务边界；
        - ``review_service``：``ArtifactReviewServiceImpl``，读取 artifact 最新评审
          的 Validator 结果（用于 evaluate）；
        - ``gate_repository``：``SqliteQualityGateRepository``，quality_gates 表 CRUD；
        - ``permission_service``：``PermissionService``，默认 CasbinPermissionService；
        - ``clock``：可注入虚拟时钟用于测试。

    评估逻辑（对应任务目标 2：QualityGate 评估）：
        - ``evaluate``：按 ``GateDefinition`` 列表评估 artifact 最新评审。
          对每个 gate：
            - 在评审的 ``validator_results`` 中找 ``validator_name == gate.validator``
              的结果；
            - 实际状态 == ``gate.required_status`` → 门禁通过；
            - 否则 → 门禁失败（Validator 缺失或状态不符）；
          - 整体：blocking 门禁全通过 → ``passed=True``；任一 blocking 失败 →
            ``passed=False``，``overall_status=REJECTED``。
        - 阻断项不能被忽略：blocking gate 失败时整体不通过（验收标准 3）。
        - 确定性：相同 (artifact 评审结果, gate_definitions) 永远得到相同决策。

    权限检查：
        - ``evaluate``：``read reviews``（读取评审结果）；
        - ``get_quality_gate``：``read reviews``；
        - ``set_quality_gate``：``write reviews``（APPROVER/ADMIN）。
    """

    def __init__(
        self,
        database: Database,
        *,
        review_service: ArtifactReviewServiceImpl | None = None,
        gate_repository: SqliteQualityGateRepository | None = None,
        permission_service: "PermissionService | None" = None,
        clock: _SystemClock | None = None,
    ) -> None:
        self._database: Database = database
        self._review_service: ArtifactReviewServiceImpl | None = review_service
        self._gate_repository: SqliteQualityGateRepository = (
            gate_repository or SqliteQualityGateRepository()
        )
        self._permission_service: "PermissionService" = (
            permission_service or CasbinPermissionService()
        )
        self._clock: _SystemClock = clock or _SystemClock()

    def _ensure_review_service(self) -> ArtifactReviewServiceImpl:
        """延迟初始化 review_service（避免循环依赖；evaluate 时才需要）。"""
        if self._review_service is None:
            self._review_service = ArtifactReviewServiceImpl(
                self._database,
                permission_service=self._permission_service,
                clock=self._clock,
            )
        return self._review_service

    # ------------------------------------------------------------------ #
    # evaluate
    # ------------------------------------------------------------------ #

    async def evaluate(
        self,
        artifact_id: str,
        *,
        gate_definitions: list[GateDefinition],
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> QualityGateResult:
        """评估 artifact 是否通过质量门禁。

        实现顺序：
            1. 校验 actor 与权限（read reviews）；
            2. 校验 gate_definitions（通过 ``validate_gate_definitions``）；
            3. 读取 artifact 最新评审的 validator_results；
            4. 对每个 gate 评估（实际状态 vs 期望状态）；
            5. 汇总：blocking 全通过 → passed=True；
            6. 写入 ``quality_gate.evaluated`` 事件；
            7. 返回 ``QualityGateResult``。

        :raises PermissionDeniedError: 无 read reviews 权限。
        :raises ArgumentError: gate_definitions 非法或 artifact_id 为空。
        :raises NotFoundError: artifact 无评审记录。
        """
        from maf_artifact_schemas.quality_gate import validate_gate_definitions

        actor_ctx, actor_user_id, org_id, trace_id = _actor_context(actor_id, actor)
        await self._permission_service.require(
            actor_ctx, ACTION_READ, RESOURCE_QUALITY_GATES
        )

        if not isinstance(artifact_id, str) or not artifact_id.strip():
            raise ArgumentError("artifact_id 不能为空")
        if not isinstance(gate_definitions, list):
            raise ArgumentError("gate_definitions 必须是 list")
        # 校验 gate_definitions（name 唯一、字段合法）
        try:
            validate_gate_definitions(gate_definitions)
        except ValueError as exc:
            raise ArgumentError(str(exc)) from exc

        artifact_id_clean = artifact_id.strip()

        # 读取 artifact 最新评审（list_reviews 按 reviewed_at 降序，取第一条）
        review_service = self._ensure_review_service()
        reviews = await review_service.list_reviews(
            artifact_id_clean,
            actor_id=actor_user_id,
            actor=actor_ctx,
        )
        if not reviews:
            raise NotFoundError(
                "artifact 无评审记录，无法评估质量门禁",
                context={"artifact_id": artifact_id_clean},
            )
        latest_review = reviews[0]
        validator_results = latest_review.get("validator_results", [])
        # 构建 validator_name → result dict 映射
        result_by_validator: dict[str, dict[str, Any]] = {}
        for vr in validator_results:
            vname = vr.get("validator_name", "")
            if vname:
                result_by_validator[vname] = vr

        # 评估每个 gate
        gate_results: list[GateResult] = []
        blocking_failed = False
        non_blocking_failed = False
        for gate in gate_definitions:
            gate_name = gate["name"]
            validator_name = gate["validator"]
            required_status = gate["required_status"]
            blocking = gate["blocking"]

            vr = result_by_validator.get(validator_name)
            if vr is None:
                # Validator 结果缺失
                gate_results.append(
                    GateResult(
                        name=gate_name,
                        passed=False,
                        validator=validator_name,
                        required_status=required_status,
                        actual_status=None,
                        blocking=blocking,
                        issues=[],
                        reason="validator_missing",
                    )
                )
                if blocking:
                    blocking_failed = True
                else:
                    non_blocking_failed = True
                continue

            actual_status = vr.get("status", "ERROR")
            issues = vr.get("issues", [])
            passed = actual_status == required_status
            reason = None if passed else "status_mismatch"
            gate_results.append(
                GateResult(
                    name=gate_name,
                    passed=passed,
                    validator=validator_name,
                    required_status=required_status,
                    actual_status=actual_status,  # type: ignore[arg-type]
                    blocking=blocking,
                    issues=issues if isinstance(issues, list) else [],
                    reason=reason,
                )
            )
            if not passed:
                if blocking:
                    blocking_failed = True
                else:
                    non_blocking_failed = True

        # 汇总整体状态
        passed = not blocking_failed
        if passed and not non_blocking_failed:
            overall_status: ReviewStatus = "APPROVED"
        elif not passed:
            overall_status = "REJECTED"
        else:
            # blocking 全通过但有非阻断门禁失败 → 请求修改
            overall_status = "CHANGES_REQUESTED"

        now = self._clock.now()
        iso = _ensure_iso(now)

        # 写入事件
        async with SqliteUnitOfWork(self._database) as uow:
            publisher = SqliteEventPublisher(uow.connection)
            await publisher.append(
                DomainEvent(
                    event_type="quality_gate.evaluated",
                    aggregate_type="quality_gate",
                    aggregate_id=artifact_id_clean,
                    organization_id=org_id,
                    project_id=None,
                    actor=ActorRef(actor_type="USER", actor_id=actor_user_id),
                    trace_id=trace_id,
                    payload={
                        "artifact_id": artifact_id_clean,
                        "review_id": latest_review["id"],
                        "passed": passed,
                        "overall_status": overall_status,
                        "gate_count": len(gate_results),
                        "gate_names": [g["name"] for g in gate_results],
                        "blocking_failed": blocking_failed,
                        "non_blocking_failed": non_blocking_failed,
                    },
                )
            )
            await uow.commit()

        return QualityGateResult(
            passed=passed,
            gate_results=gate_results,
            overall_status=overall_status,
            artifact_id=artifact_id_clean,
            evaluated_at=iso,
        )

    # ------------------------------------------------------------------ #
    # get_quality_gate / set_quality_gate
    # ------------------------------------------------------------------ #

    async def get_quality_gate(
        self,
        run_id: str,
        node_id: str | None = None,
        *,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> QualityGateConfig:
        """获取 Run/Node 的质量门禁配置。

        :raises PermissionDeniedError: 无 read reviews 权限。
        :raises NotFoundError: 配置不存在。
        """
        actor_ctx, _, _, _ = _actor_context(actor_id, actor)
        await self._permission_service.require(
            actor_ctx, ACTION_READ, RESOURCE_QUALITY_GATES
        )

        if not isinstance(run_id, str) or not run_id.strip():
            raise ArgumentError("run_id 不能为空")

        async with SqliteUnitOfWork(self._database) as uow:
            rec = await self._gate_repository.get_gate(
                uow.connection, run_id.strip(), node_id
            )
            await uow.rollback()

        if rec is None:
            raise NotFoundError(
                "质量门禁配置不存在",
                context={"run_id": run_id, "node_id": node_id},
            )
        return gate_record_to_view(rec)

    async def set_quality_gate(
        self,
        run_id: str,
        gate_definitions: list[GateDefinition],
        *,
        actor_id: str,
        actor: ActorContext | None = None,
        node_id: str | None = None,
    ) -> QualityGateConfig:
        """设置 Run 的质量门禁配置（覆盖旧配置）。

        :raises PermissionDeniedError: 无 write reviews 权限（需 APPROVER/ADMIN）。
        :raises ArgumentError: gate_definitions 非法。
        """
        from maf_artifact_schemas.quality_gate import validate_gate_definitions

        actor_ctx, actor_user_id, _, _ = _actor_context(actor_id, actor)
        await self._permission_service.require(
            actor_ctx, ACTION_WRITE, RESOURCE_QUALITY_GATES
        )

        if not isinstance(run_id, str) or not run_id.strip():
            raise ArgumentError("run_id 不能为空")
        if not isinstance(gate_definitions, list):
            raise ArgumentError("gate_definitions 必须是 list")
        try:
            validate_gate_definitions(gate_definitions)
        except ValueError as exc:
            raise ArgumentError(str(exc)) from exc

        now = self._clock.now()
        iso = _ensure_iso(now)
        config_id = new_gate_config_id()
        # 序列化为 list[dict] 供 JSON 存储
        defs_as_dicts: list[dict[str, Any]] = [
            dict(g) if not isinstance(g, dict) else g for g in gate_definitions
        ]

        async with SqliteUnitOfWork(self._database) as uow:
            await self._gate_repository.upsert_gate(
                uow.connection,
                config_id=config_id,
                run_id=run_id.strip(),
                node_id=node_id,
                gate_definitions=defs_as_dicts,
                created_by=actor_user_id,
                created_at=iso,
            )
            await uow.commit()

        # 重新读取返回
        async with SqliteUnitOfWork(self._database) as uow:
            rec = await self._gate_repository.get_gate(
                uow.connection, run_id.strip(), node_id
            )
            await uow.rollback()
        assert rec is not None
        return gate_record_to_view(rec)


# --------------------------------------------------------------------------- #
# PermissionService Protocol（与 artifacts.service.PermissionService 对齐）
# --------------------------------------------------------------------------- #


class PermissionService(Protocol):
    """权限检查协议（与 ``maf_policy.CasbinPermissionService`` 对齐）。"""

    async def require(
        self, actor: ActorContext, action: str, resource: str
    ) -> None: ...


# --------------------------------------------------------------------------- #
# TASK-081：ReviewService / QualityGateService Protocol
# --------------------------------------------------------------------------- #


class ReviewService(Protocol):
    """评审服务协议（TASK-081）。

    对应任务目标 1：submit/get/list/approve/reject/request_changes。
    所有方法通过 ``PermissionService.require`` 校验权限。
    """

    async def submit_review(
        self,
        artifact_id: str,
        validator_results: list[ValidatorResult],
        *,
        actor_id: str,
        comment: str | None = None,
    ) -> ArtifactReviewView:
        """提交评审（含验证结果与人工评论），review_status 初始为 PENDING。"""
        ...

    async def get_review(
        self, review_id: str, *, actor_id: str
    ) -> ArtifactReviewView:
        """获取评审详情。"""
        ...

    async def list_reviews(
        self,
        artifact_id: str,
        *,
        actor_id: str,
        status: ReviewStatus | None = None,
    ) -> list[ArtifactReviewView]:
        """列出 artifact 的评审（可按 review_status 过滤）。"""
        ...

    async def approve_review(
        self, review_id: str, *, actor_id: str, comment: str
    ) -> ArtifactReviewView:
        """人工批准评审（PENDING/CHANGES_REQUESTED → APPROVED）。"""
        ...

    async def reject_review(
        self, review_id: str, *, actor_id: str, comment: str
    ) -> ArtifactReviewView:
        """人工拒绝评审（PENDING/CHANGES_REQUESTED → REJECTED）。"""
        ...

    async def request_changes(
        self, review_id: str, *, actor_id: str, comment: str
    ) -> ArtifactReviewView:
        """请求修改（PENDING → CHANGES_REQUESTED）。"""
        ...


class QualityGateService(Protocol):
    """Quality Gate 服务协议（TASK-081）。

    对应任务目标 2：evaluate/get/set。
    阻断项不能被忽略：blocking 门禁失败时整体不通过。
    """

    async def evaluate(
        self,
        artifact_id: str,
        *,
        gate_definitions: list[GateDefinition],
        actor_id: str,
    ) -> QualityGateResult:
        """评估 artifact 是否通过质量门禁。

        检查 artifact 最新评审的 Validator 结果是否满足每个 gate 的
        required_status；blocking 门禁失败时整体 passed=False。
        不得让 LLM 自行决定是否忽略阻断项；相同输入必须得到相同决策。
        """
        ...

    async def get_quality_gate(
        self, run_id: str, node_id: str | None = None, *, actor_id: str
    ) -> QualityGateConfig:
        """获取 Run/Node 的质量门禁配置。"""
        ...

    async def set_quality_gate(
        self,
        run_id: str,
        gate_definitions: list[GateDefinition],
        *,
        actor_id: str,
        node_id: str | None = None,
    ) -> QualityGateConfig:
        """设置质量门禁配置。"""
        ...


# --------------------------------------------------------------------------- #
# 模块级工具：建表
# --------------------------------------------------------------------------- #


async def ensure_reviews_schema(database: Database) -> None:
    """在 ``database`` 上创建 ``artifact_reviews`` 与 ``quality_gates`` 表（幂等）。

    供测试与开发期首次启动使用；正式部署由 ``migrations/`` 顺序迁移负责。
    """
    from .repository import init_artifact_reviews_schema, init_quality_gates_schema

    async with SqliteUnitOfWork(database) as uow:
        await init_artifact_reviews_schema(uow.connection)
        await init_quality_gates_schema(uow.connection)
        await uow.commit()


__all__ = [
    "ACTION_READ",
    "ACTION_WRITE",
    "ArtifactReviewServiceImpl",
    "PermissionService",
    "QualityGateService",
    "QualityGateServiceImpl",
    "RESOURCE_QUALITY_GATES",
    "ReviewService",
    "ensure_reviews_schema",
]
