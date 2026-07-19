"""Review 查询和 Quality Gate 契约。

TASK-080 增量（确定性 Validator 框架配套）：
- ``ArtifactReviewStatus``：artifact 评审状态字面量（PASS/FAIL/ERROR），
  与 ``ValidatorStatus`` 对齐。ERROR 表示 Validator 自身出错，必须视为失败，
  不能降级为 PASS。
- ``ArtifactReviewView``：``ArtifactReviewServiceImpl`` 提交/获取/列表返回的
  评审视图，对应 ``artifact_reviews`` 表行。``validator_results`` 是
  ``ValidatorResult.to_dict()`` 列表的 JSON 反序列化结果。

TASK-081 增量（Review 与 QualityGate 核心实现）：
- ``ReviewStatus``：评审工作流状态字面量（PENDING/APPROVED/REJECTED/
  CHANGES_REQUESTED），与 ``ArtifactReviewStatus``（PASS/FAIL/ERROR，Validator
  汇总状态）正交。``ArtifactReviewView`` 新增 ``review_status`` 与
  ``reviewer_comment`` 字段，记录人工评审决策与评论。
- ``GateDefinition``：Quality Gate 定义（name/validator/required_status/blocking），
  替换 TASK-081 占位版本。
- ``GateResult`` / ``QualityGateResult`` / ``QualityGateConfig``：Quality Gate
  评估结果与配置视图，对应 ``quality_gates`` 表。
"""

from typing import Any, Literal, TypedDict


# --------------------------------------------------------------------------- #
# TASK-080：artifact_reviews 表对应视图
# --------------------------------------------------------------------------- #


#: artifact 评审状态字面量（与 ``ValidatorStatus`` 对齐）。
#: - ``PASS``：所有 Validator 通过；
#: - ``FAIL``：至少一个 Validator 返回 FAIL（校验发现阻断问题）；
#: - ``ERROR``：至少一个 Validator 返回 ERROR（Validator 自身出错），
#:   必须视为失败，不能降级为 PASS（验收标准 1）。
ArtifactReviewStatus = Literal["PASS", "FAIL", "ERROR"]


#: 评审工作流状态字面量（TASK-081）。
#: - ``PENDING``：已提交评审（含 Validator 结果），等待人工决策；
#: - ``APPROVED``：人工批准评审；
#: - ``REJECTED``：人工拒绝评审；
#: - ``CHANGES_REQUESTED``：请求修改后重新提交。
#:
#: 与 ``ArtifactReviewStatus``（PASS/FAIL/ERROR，Validator 汇总状态）正交：
#: ``status`` 描述 Validator 自动校验结果，``review_status`` 描述人工评审决策。
ReviewStatus = Literal["PENDING", "APPROVED", "REJECTED", "CHANGES_REQUESTED"]


class ArtifactReviewView(TypedDict):
    """``artifact_reviews`` 表行对外视图（TASK-080 + TASK-081）。

    TASK-080 字段：
    - ``id``：评审记录 ID（UUID）；
    - ``artifact_id``：被评审的 artifact ID；
    - ``status``：Validator 汇总状态（由 ``aggregate_review_status`` 汇总）；
    - ``validator_results``：``ValidatorResult.to_dict()`` 列表，保留每个
      Validator 的 ``status``/``issues``/``validator_name``/``validated_at``；
    - ``reviewer``：提交评审的 actor_id；
    - ``reviewed_at``：提交时间（ISO8601 带 UTC）；
    - ``version_no``：乐观锁版本号。

    TASK-081 字段：
    - ``review_status``：人工评审工作流状态（PENDING/APPROVED/REJECTED/
      CHANGES_REQUESTED）；``submit_review`` 时为 PENDING，
      ``approve_review``/``reject_review``/``request_changes`` 转换状态；
    - ``reviewer_comment``：人工评审评论（submit 时可空，approve/reject/
      request_changes 时必填）；
    - ``decided_by``：做出人工决策的 actor_id（submit 时与 reviewer 相同，
      approve/reject/request_changes 时为决策者）；
    - ``decided_at``：人工决策时间（submit 时与 reviewed_at 相同）。
    """

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


# --------------------------------------------------------------------------- #
# TASK-081：Quality Gate 契约
# --------------------------------------------------------------------------- #


#: Validator 状态字面量（与 ``artifacts.service.ValidatorStatus`` 对齐）。
ValidatorStatusLiteral = Literal["PASS", "FAIL", "ERROR"]


class GateDefinition(TypedDict):
    """单个 Quality Gate 的定义（TASK-081）。

    - ``name``：门禁名称（如 ``"schema_validation"``、``"size_limit"``），
      在同一 ``gate_definitions`` 列表中唯一；
    - ``validator``：使用的 Validator 名称（与 ``ValidatorResult.validator_name``
      匹配，如 ``"json_schema:task_payload:v1"``、``"size_limit"``）；
    - ``required_status``：期望的 Validator 状态（PASS/FAIL/ERROR）。当 artifact
      评审中该 Validator 的实际状态等于 ``required_status`` 时门禁通过；
    - ``blocking``：是否阻断门禁。``True`` 时该门禁失败会导致整体不通过；
      ``False`` 时仅记录警告，不影响整体通过判定。
    """

    name: str
    validator: str
    required_status: ValidatorStatusLiteral
    blocking: bool


class GateResult(TypedDict):
    """单个 Quality Gate 的评估结果。

    - ``name``：门禁名称（与 ``GateDefinition.name`` 一致）；
    - ``passed``：该门禁是否通过（实际状态 == 期望状态）；
    - ``validator``：评估的 Validator 名称；
    - ``required_status``：期望状态；
    - ``actual_status``：实际状态（Validator 结果中的 ``status``）；
      Validator 结果缺失时为 ``None``；
    - ``blocking``：是否阻断门禁；
    - ``issues``：该 Validator 的 issues 列表（从 ``ValidatorResult.issues``
      序列化），用于说明失败原因；
    - ``reason``：门禁失败原因（如 ``"validator_missing"``、
      ``"status_mismatch"``），通过时为 ``None``。
    """

    name: str
    passed: bool
    validator: str
    required_status: ValidatorStatusLiteral
    actual_status: ValidatorStatusLiteral | None
    blocking: bool
    issues: list[dict[str, Any]]
    reason: str | None


class QualityGateResult(TypedDict):
    """Quality Gate 整体评估结果。

    - ``passed``：是否通过所有 ``blocking`` 门禁（blocking 门禁全通过 → True）；
    - ``gate_results``：每个门禁的评估结果列表（按 ``gate_definitions`` 顺序）；
    - ``overall_status``：整体评审状态（与 ``ReviewStatus`` 对齐）：
      - ``APPROVED``：所有门禁通过（含非阻断）；
      - ``CHANGES_REQUESTED``：所有 blocking 门禁通过，但有非阻断门禁失败；
      - ``REJECTED``：至少一个 blocking 门禁失败。
    - ``artifact_id``：被评估的 artifact ID；
    - ``evaluated_at``：评估时间（ISO8601 带 UTC）。
    """

    passed: bool
    gate_results: list[GateResult]
    overall_status: ReviewStatus
    artifact_id: str
    evaluated_at: str


class QualityGateConfig(TypedDict):
    """Quality Gate 配置视图（对应 ``quality_gates`` 表行）。

    - ``id``：配置记录 ID（UUID）；
    - ``run_id``：Run ID；
    - ``node_id``：Node ID（可空，``None`` 表示 Run 级别门禁）；
    - ``gate_definitions``：``GateDefinition`` 列表；
    - ``created_by``：创建者 actor_id；
    - ``created_at``：创建时间（ISO8601 带 UTC）；
    - ``version_no``：乐观锁版本号。
    """

    id: str
    run_id: str
    node_id: str | None
    gate_definitions: list[GateDefinition]
    created_by: str
    created_at: str
    version_no: int


# --------------------------------------------------------------------------- #
# TASK-081 保留：通用 Review 查询契约（供未来 TASK 使用，本任务不实现查询语义）
# --------------------------------------------------------------------------- #


class ReviewQuery(TypedDict, total=False):
    project_id: str
    run_id: str
    review_type: str
    status: str
    cursor: str
    limit: int


class ReviewView(TypedDict):
    id: str
    run_id: str
    task_id: str | None
    review_type: Literal["ARCHITECTURE", "CODE", "TEST", "ACCEPTANCE", "HUMAN"]
    reviewer_role_version_id: str | None
    status: Literal["PENDING", "PASS", "FAIL", "CHANGES_REQUESTED"]
    blocking_items: list[dict[str, Any]]
    warning_items: list[dict[str, Any]]
    evidence_artifact_version_ids: list[str]
    completed_at: str | None


class ReviewPage(TypedDict):
    items: list[ReviewView]
    next_cursor: str | None
    has_more: bool


class GateInputs(TypedDict):
    run_id: str
    source_node_key: str
    review_ids: list[str]
    artifact_version_ids: list[str]


class GateDecisionView(TypedDict):
    gate_node_key: str
    decision: Literal["PASS", "REWORK", "WAITING_HUMAN", "FAIL"]
    review_ids: list[str]
    blocking_items: list[dict[str, Any]]
    warning_items: list[dict[str, Any]]
    rework_category: str | None
    target_node_key: str | None
    affected_node_keys: list[str]
    evidence_refs: list[str]


__all__ = [
    "ArtifactReviewStatus",
    "ArtifactReviewView",
    "GateDecisionView",
    "GateDefinition",
    "GateInputs",
    "GateResult",
    "QualityGateConfig",
    "QualityGateResult",
    "ReviewPage",
    "ReviewQuery",
    "ReviewStatus",
    "ReviewView",
    "ValidatorStatusLiteral",
]
