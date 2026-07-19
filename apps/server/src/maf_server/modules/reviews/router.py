"""Review 与 Quality Gate 公共 HTTP 接口。

TASK-081 增量（Review 与 QualityGate REST API）：
- ``build_review_router`` 工厂构造 FastAPI ``APIRouter``，挂载 artifact-reviews
  与 quality-gates 两组路由：
  - ``POST /api/v1/artifact-reviews``：提交评审（含 ValidatorResult 与可选 comment）；
  - ``GET /api/v1/artifact-reviews/{review_id}``：获取评审详情；
  - ``GET /api/v1/artifact-reviews?artifact_id=...&status=...``：列出 artifact 的
    评审（可按 review_status 过滤）；
  - ``POST /api/v1/artifact-reviews/{review_id}/approve``：人工批准；
  - ``POST /api/v1/artifact-reviews/{review_id}/reject``：人工拒绝；
  - ``POST /api/v1/artifact-reviews/{review_id}/request-changes``：请求修改；
  - ``POST /api/v1/quality-gates/evaluate``：评估质量门禁；
  - ``GET /api/v1/quality-gates?run_id=...&node_id=...``：获取质量门禁配置；
  - ``PUT /api/v1/quality-gates``：设置质量门禁配置（覆盖旧配置）。

设计原则（与 ``artifacts/router.py`` 一致）：
- router 是 service 的薄包装，只做 HTTP ↔ TypedDict 转换，不含业务逻辑；
- 使用 Pydantic 模型对外暴露 schema，内部用 ``ActorDep`` 注入认证上下文；
- 不暴露宿主绝对路径与内部 Repository 实现细节；
- ``actor`` 依赖为占位实现（正式实现应在 ``api/dependencies.py`` 中解析
  Cookie/Authorization → 构造 ``ActorContext``）。

保留：``ReviewHttpApi`` Protocol（TASK-081 之前占位，供未来通用 review 查询使用）。
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from fastapi import APIRouter, Body, Depends, Query, status
from pydantic import BaseModel, Field
from typing_extensions import Annotated

from maf_contracts.common import ActorContext

from .schemas import (
    ArtifactReviewView,
    GateDefinition,
    GateResult,
    QualityGateConfig,
    QualityGateResult,
    ReviewPage,
    ReviewQuery,
    ReviewStatus,
    ReviewView,
)

# --------------------------------------------------------------------------- #
# Pydantic 对外模型
# --------------------------------------------------------------------------- #


#: ValidatorResult 中 issue 的对外模型字段（与 artifacts.service.ValidatorIssue 对齐）。
ValidatorIssueOut = dict[str, Any]


class ValidatorResultOut(BaseModel):
    """``ValidatorResult`` 对外视图（与 ``artifacts.service.ValidatorResult.to_dict``
    对齐）。"""

    status: Literal["PASS", "FAIL", "ERROR"]
    issues: list[dict[str, Any]] = Field(default_factory=list)
    validator_name: str
    validated_at: str


class ArtifactReviewOut(BaseModel):
    """``ArtifactReviewView`` 对外模型。"""

    id: str
    artifact_id: str
    status: Literal["PASS", "FAIL", "ERROR"]
    validator_results: list[dict[str, Any]]
    reviewer: str
    reviewed_at: str
    version_no: int = Field(ge=1)
    review_status: Literal["PENDING", "APPROVED", "REJECTED", "CHANGES_REQUESTED"]
    reviewer_comment: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None


def _review_to_out(view: ArtifactReviewView) -> ArtifactReviewOut:
    """把 ``ArtifactReviewView`` TypedDict 映射为 Pydantic ``ArtifactReviewOut``。"""
    return ArtifactReviewOut(
        id=view["id"],
        artifact_id=view["artifact_id"],
        status=view["status"],
        validator_results=view["validator_results"],
        reviewer=view["reviewer"],
        reviewed_at=view["reviewed_at"],
        version_no=view["version_no"],
        review_status=view["review_status"],
        reviewer_comment=view["reviewer_comment"],
        decided_by=view["decided_by"],
        decided_at=view["decided_at"],
    )


class SubmitReviewIn(BaseModel):
    """``POST /api/v1/artifact-reviews`` 请求体。"""

    artifact_id: str = Field(min_length=1)
    validator_results: list[dict[str, Any]] = Field(min_length=0)
    comment: str | None = Field(default=None, max_length=4096)


class ReviewDecisionIn(BaseModel):
    """``approve``/``reject``/``request-changes`` 请求体（人工决策）。"""

    comment: str = Field(min_length=1, max_length=4096, description="决策评论（必填）")


# --------------------------------------------------------------------------- #
# Quality Gate Pydantic 模型
# --------------------------------------------------------------------------- #


class GateDefinitionModel(BaseModel):
    """单个 Quality Gate 定义（与 ``GateDefinition`` TypedDict 对齐）。"""

    name: str = Field(min_length=1, max_length=64)
    validator: str = Field(min_length=1, max_length=128)
    required_status: Literal["PASS", "FAIL", "ERROR"]
    blocking: bool


class GateResultOut(BaseModel):
    """单个 Quality Gate 评估结果。"""

    name: str
    passed: bool
    validator: str
    required_status: Literal["PASS", "FAIL", "ERROR"]
    actual_status: Literal["PASS", "FAIL", "ERROR"] | None = None
    blocking: bool
    issues: list[dict[str, Any]] = Field(default_factory=list)
    reason: str | None = None


class QualityGateResultOut(BaseModel):
    """``QualityGateResult`` 对外模型。"""

    passed: bool
    gate_results: list[GateResultOut]
    overall_status: Literal["PENDING", "APPROVED", "REJECTED", "CHANGES_REQUESTED"]
    artifact_id: str
    evaluated_at: str


def _gate_result_to_out(g: GateResult) -> GateResultOut:
    return GateResultOut(
        name=g["name"],
        passed=g["passed"],
        validator=g["validator"],
        required_status=g["required_status"],
        actual_status=g["actual_status"],
        blocking=g["blocking"],
        issues=g["issues"],
        reason=g["reason"],
    )


def _quality_gate_result_to_out(
    result: QualityGateResult,
) -> QualityGateResultOut:
    return QualityGateResultOut(
        passed=result["passed"],
        gate_results=[_gate_result_to_out(g) for g in result["gate_results"]],
        overall_status=result["overall_status"],
        artifact_id=result["artifact_id"],
        evaluated_at=result["evaluated_at"],
    )


class QualityGateConfigOut(BaseModel):
    """``QualityGateConfig`` 对外模型。"""

    id: str
    run_id: str
    node_id: str | None = None
    gate_definitions: list[GateDefinitionModel]
    created_by: str
    created_at: str
    version_no: int = Field(ge=1)


def _gate_config_to_out(cfg: QualityGateConfig) -> QualityGateConfigOut:
    return QualityGateConfigOut(
        id=cfg["id"],
        run_id=cfg["run_id"],
        node_id=cfg["node_id"],
        gate_definitions=[
            GateDefinitionModel(**d) for d in cfg["gate_definitions"]
        ],
        created_by=cfg["created_by"],
        created_at=cfg["created_at"],
        version_no=cfg["version_no"],
    )


class EvaluateQualityGateIn(BaseModel):
    """``POST /api/v1/quality-gates/evaluate`` 请求体。"""

    artifact_id: str = Field(min_length=1)
    gate_definitions: list[GateDefinitionModel] = Field(min_length=1)


class SetQualityGateIn(BaseModel):
    """``PUT /api/v1/quality-gates`` 请求体。"""

    run_id: str = Field(min_length=1)
    node_id: str | None = None
    gate_definitions: list[GateDefinitionModel] = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Service 鸭子类型
# --------------------------------------------------------------------------- #


class ReviewServiceLike(Protocol):
    """``build_review_router`` 接受的 review service 鸭子类型（TASK-081）。"""

    async def submit_review(
        self,
        artifact_id: str,
        validator_results: list[dict[str, Any]],
        *,
        actor_id: str,
        actor: ActorContext | None = None,
        comment: str | None = None,
    ) -> ArtifactReviewView: ...

    async def get_review(
        self,
        review_id: str,
        *,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> ArtifactReviewView: ...

    async def list_reviews(
        self,
        artifact_id: str,
        *,
        actor_id: str,
        actor: ActorContext | None = None,
        status: ReviewStatus | None = None,
    ) -> list[ArtifactReviewView]: ...

    async def approve_review(
        self,
        review_id: str,
        *,
        actor_id: str,
        comment: str,
        actor: ActorContext | None = None,
    ) -> ArtifactReviewView: ...

    async def reject_review(
        self,
        review_id: str,
        *,
        actor_id: str,
        comment: str,
        actor: ActorContext | None = None,
    ) -> ArtifactReviewView: ...

    async def request_changes(
        self,
        review_id: str,
        *,
        actor_id: str,
        comment: str,
        actor: ActorContext | None = None,
    ) -> ArtifactReviewView: ...


class QualityGateServiceLike(Protocol):
    """``build_review_router`` 接受的 quality gate service 鸭子类型（TASK-081）。"""

    async def evaluate(
        self,
        artifact_id: str,
        *,
        gate_definitions: list[GateDefinition],
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> QualityGateResult: ...

    async def get_quality_gate(
        self,
        run_id: str,
        node_id: str | None = None,
        *,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> QualityGateConfig: ...

    async def set_quality_gate(
        self,
        run_id: str,
        gate_definitions: list[GateDefinition],
        *,
        actor_id: str,
        actor: ActorContext | None = None,
        node_id: str | None = None,
    ) -> QualityGateConfig: ...


# --------------------------------------------------------------------------- #
# actor 依赖占位（正式实现应在 api/dependencies.py）
# --------------------------------------------------------------------------- #


async def _anonymous_actor_dependency() -> ActorContext:
    """占位 actor 依赖；正式实现应解析 Cookie/Authorization 构造 ActorContext。"""
    return ActorContext(
        user_id="",
        organization_id="system",
        permission_keys=[],
        trace_id="",
    )


ActorDep = Annotated[ActorContext, Depends(_anonymous_actor_dependency)]


# --------------------------------------------------------------------------- #
# 路由工厂
# --------------------------------------------------------------------------- #


def build_review_router(
    review_service: ReviewServiceLike,
    gate_service: QualityGateServiceLike | None = None,
) -> APIRouter:
    """构造 Review/QualityGate FastAPI ``APIRouter``。

    路由（TASK-081）：
        - ``POST /api/v1/artifact-reviews``：提交评审；
        - ``GET /api/v1/artifact-reviews/{review_id}``：获取评审详情；
        - ``GET /api/v1/artifact-reviews?artifact_id=...&status=...``：列出评审；
        - ``POST /api/v1/artifact-reviews/{review_id}/approve``：人工批准；
        - ``POST /api/v1/artifact-reviews/{review_id}/reject``：人工拒绝；
        - ``POST /api/v1/artifact-reviews/{review_id}/request-changes``：请求修改；
        - ``POST /api/v1/quality-gates/evaluate``：评估质量门禁；
        - ``GET /api/v1/quality-gates?run_id=...&node_id=...``：获取质量门禁配置；
        - ``PUT /api/v1/quality-gates``：设置质量门禁配置。

    :param review_service: ``ReviewService`` 实现。
    :param gate_service: 可选的 ``QualityGateService`` 实现；传入后才会注册
        QualityGate 相关路由。
    :returns: FastAPI ``APIRouter``，由调用方挂载到 app。
    """
    router = APIRouter(prefix="/api/v1/artifact-reviews", tags=["reviews"])

    # ------------------------------------------------------------------ #
    # Review 端点
    # ------------------------------------------------------------------ #

    @router.post(
        "",
        response_model=ArtifactReviewOut,
        status_code=status.HTTP_201_CREATED,
        summary="提交 Artifact 评审",
    )
    async def submit_review(
        actor: ActorDep,
        body: SubmitReviewIn = Body(...),
    ) -> ArtifactReviewOut:
        """提交 artifact 的 Validator 校验结果与可选评论，review_status=PENDING。"""
        view = await review_service.submit_review(
            artifact_id=body.artifact_id,
            validator_results=body.validator_results,
            actor_id=actor.get("user_id", ""),
            actor=actor,
            comment=body.comment,
        )
        return _review_to_out(view)

    @router.get(
        "/{review_id}",
        response_model=ArtifactReviewOut,
        summary="获取评审详情",
    )
    async def get_review(
        actor: ActorDep,
        review_id: str,
    ) -> ArtifactReviewOut:
        """按 review_id 获取评审记录。"""
        view = await review_service.get_review(
            review_id=review_id,
            actor_id=actor.get("user_id", ""),
            actor=actor,
        )
        return _review_to_out(view)

    @router.get(
        "",
        response_model=list[ArtifactReviewOut],
        summary="列出 Artifact 的评审记录",
    )
    async def list_reviews(
        actor: ActorDep,
        artifact_id: str = Query(..., min_length=1),
        review_status: (
            Literal["PENDING", "APPROVED", "REJECTED", "CHANGES_REQUESTED"] | None
        ) = Query(default=None, alias="status"),
    ) -> list[ArtifactReviewOut]:
        """按 artifact_id 列出评审记录（可按 review_status 过滤）。"""
        views = await review_service.list_reviews(
            artifact_id=artifact_id,
            actor_id=actor.get("user_id", ""),
            actor=actor,
            status=review_status,
        )
        return [_review_to_out(v) for v in views]

    @router.post(
        "/{review_id}/approve",
        response_model=ArtifactReviewOut,
        summary="人工批准评审",
    )
    async def approve_review(
        actor: ActorDep,
        review_id: str,
        body: ReviewDecisionIn = Body(...),
    ) -> ArtifactReviewOut:
        """人工批准评审（PENDING/CHANGES_REQUESTED → APPROVED）。"""
        view = await review_service.approve_review(
            review_id=review_id,
            actor_id=actor.get("user_id", ""),
            comment=body.comment,
            actor=actor,
        )
        return _review_to_out(view)

    @router.post(
        "/{review_id}/reject",
        response_model=ArtifactReviewOut,
        summary="人工拒绝评审",
    )
    async def reject_review(
        actor: ActorDep,
        review_id: str,
        body: ReviewDecisionIn = Body(...),
    ) -> ArtifactReviewOut:
        """人工拒绝评审（PENDING/CHANGES_REQUESTED → REJECTED）。"""
        view = await review_service.reject_review(
            review_id=review_id,
            actor_id=actor.get("user_id", ""),
            comment=body.comment,
            actor=actor,
        )
        return _review_to_out(view)

    @router.post(
        "/{review_id}/request-changes",
        response_model=ArtifactReviewOut,
        summary="请求修改评审",
    )
    async def request_changes(
        actor: ActorDep,
        review_id: str,
        body: ReviewDecisionIn = Body(...),
    ) -> ArtifactReviewOut:
        """请求修改（PENDING → CHANGES_REQUESTED）。"""
        view = await review_service.request_changes(
            review_id=review_id,
            actor_id=actor.get("user_id", ""),
            comment=body.comment,
            actor=actor,
        )
        return _review_to_out(view)

    # ------------------------------------------------------------------ #
    # Quality Gate 端点（需传入 gate_service）
    # ------------------------------------------------------------------ #
    if gate_service is not None:
        gates_router = APIRouter(
            prefix="/api/v1/quality-gates", tags=["quality-gates"]
        )

        @gates_router.post(
            "/evaluate",
            response_model=QualityGateResultOut,
            summary="评估质量门禁",
        )
        async def evaluate_quality_gate(
            actor: ActorDep,
            body: EvaluateQualityGateIn = Body(...),
        ) -> QualityGateResultOut:
            """评估 artifact 是否通过质量门禁（blocking 失败 → 整体不通过）。"""
            gate_defs: list[GateDefinition] = [
                g.model_dump() for g in body.gate_definitions
            ]
            result = await gate_service.evaluate(
                artifact_id=body.artifact_id,
                gate_definitions=gate_defs,
                actor_id=actor.get("user_id", ""),
                actor=actor,
            )
            return _quality_gate_result_to_out(result)

        @gates_router.get(
            "",
            response_model=QualityGateConfigOut,
            summary="获取质量门禁配置",
        )
        async def get_quality_gate(
            actor: ActorDep,
            run_id: str = Query(..., min_length=1),
            node_id: str | None = Query(default=None),
        ) -> QualityGateConfigOut:
            """按 (run_id, node_id) 获取质量门禁配置。"""
            cfg = await gate_service.get_quality_gate(
                run_id=run_id,
                node_id=node_id,
                actor_id=actor.get("user_id", ""),
                actor=actor,
            )
            return _gate_config_to_out(cfg)

        @gates_router.put(
            "",
            response_model=QualityGateConfigOut,
            summary="设置质量门禁配置",
        )
        async def set_quality_gate(
            actor: ActorDep,
            body: SetQualityGateIn = Body(...),
        ) -> QualityGateConfigOut:
            """设置 Run/Node 的质量门禁配置（覆盖旧配置）。"""
            gate_defs: list[GateDefinition] = [
                g.model_dump() for g in body.gate_definitions
            ]
            cfg = await gate_service.set_quality_gate(
                run_id=body.run_id,
                gate_definitions=gate_defs,
                actor_id=actor.get("user_id", ""),
                actor=actor,
                node_id=body.node_id,
            )
            return _gate_config_to_out(cfg)

        router.include_router(gates_router)

    return router


# --------------------------------------------------------------------------- #
# 保留：通用 ReviewHttpApi Protocol（占位，供未来 TASK 使用）
# --------------------------------------------------------------------------- #


class ReviewHttpApi(Protocol):
    """通用 review 查询接口（占位，供未来 TASK 使用）。

    TASK-081 不实现该接口，保留供未来通用 review 查询使用。
    """

    async def get_reviews(self, actor: ActorContext, query: ReviewQuery) -> ReviewPage:
        """GET `/api/v1/reviews`；按权限过滤的游标分页查询。"""
        ...


__all__ = [
    "ArtifactReviewOut",
    "EvaluateQualityGateIn",
    "GateDefinitionModel",
    "GateResultOut",
    "QualityGateConfigOut",
    "QualityGateResultOut",
    "QualityGateServiceLike",
    "ReviewHttpApi",
    "ReviewServiceLike",
    "ReviewView",
    "SetQualityGateIn",
    "SubmitReviewIn",
    "build_review_router",
]
