"""GitHub 跨节点协调使用的机器可读契约。"""

from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field


TaskStatus = Literal[
    "PLANNED", "READY", "ASSIGNED", "IN_PROGRESS", "BLOCKED", "SUBMITTED",
    "REVIEWING", "REWORK_REQUIRED", "LEASE_EXPIRED", "DONE", "FAILED", "CANCELLED",
]

CoordinationEventType = Literal[
    "NODE_REGISTERED", "NODE_UPDATED", "CLAIM_REQUESTED", "PROGRESS_REPORTED",
    "BLOCKED_REPORTED", "SUBMISSION_CREATED", "WORK_ABANDONED",
]

#: 节点事件分支前缀（``maf/node/<node-id>``），对应《GitHub 分布式协作协议》§2。
NODE_BRANCH_PREFIX: str = "maf/node/"

#: 事件文件在节点分支上的目录前缀（与《系统设计文档》§9 对齐）。
_EVENT_DIR_PREFIX: str = ".maf/events/"


class NodeManifest(TypedDict):
    schema_version: int
    node_id: str
    display_name: str
    git_identity: dict[str, str]
    capabilities: list[str]
    model_aliases: list[str]
    docker_profiles: list[str]
    capacity: int
    status: Literal["ACTIVE", "DRAINING", "OFFLINE", "QUARANTINED"]
    software_version: str
    version: int


class TaskAssignment(TypedDict):
    node_id: str
    assignment_id: str
    assignment_epoch: int
    assigned_at: str
    expires_at: str
    based_on_control_commit: str


class TaskProgress(TypedDict):
    percent: int
    completed_items: list[str]
    remaining_items: list[str]
    problems: list[dict[str, Any]]
    current_head_commit: str | None
    test_summary: str | None
    last_reported_at: str | None


class TaskDelivery(TypedDict):
    branch: str | None
    base_commit: str | None
    head_commit: str | None
    pull_request_url: str | None
    changed_paths: list[str]
    test_report_path: str | None
    known_issues: list[str]


class CoordinationTask(TypedDict):
    schema_version: int
    task_id: str
    parent_task_id: str | None
    title: str
    description: str
    status: TaskStatus
    priority: int
    requirements: dict[str, Any]
    dependencies: list[str]
    assignment: TaskAssignment | None
    progress: TaskProgress
    delivery: TaskDelivery
    version: int


class CoordinationEvent(TypedDict):
    schema_version: int
    event_id: str
    event_type: CoordinationEventType
    node_id: str
    task_id: str | None
    assignment_id: str | None
    assignment_epoch: int | None
    based_on_control_commit: str
    occurred_at: str
    payload: dict[str, Any]


class EventDecision(TypedDict):
    event_id: str
    accepted: bool
    reason_code: str
    control_commit: str | None
    resulting_task_status: NotRequired[TaskStatus]


class CoordinationSnapshot(TypedDict):
    project_id: str
    control_commit: str
    tasks: list[CoordinationTask]
    nodes: list[NodeManifest]
    generated_at: str


# --------------------------------------------------------------------------- #
# TASK-018: 事件分支写入校验模型与辅助函数
# --------------------------------------------------------------------------- #


class CoordinationEventModel(BaseModel):
    """``CoordinationEvent`` 的 Pydantic 校验模型。

    与 ``templates/git_coordination/schemas/event-v1.schema.json`` 对齐：
    字段、类型、约束一致。``RunnerGitClient.append_event`` 在写入节点分支前
    用本模型校验事件 dict，确保 wire format 合规。

    与 :class:`CoordinationEvent` (TypedDict) 的关系：

    - ``CoordinationEvent`` 是线格式契约（``dict``），由 ``RunnerRegistry``
      等模块构造，直接序列化为 JSON 文件；
    - ``CoordinationEventModel`` 是校验模型（``pydantic.BaseModel``），在写入
      前验证 dict，``additionalProperties: false`` 与 Schema 一致。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(ge=1, le=1)
    event_id: str = Field(min_length=16)
    event_type: CoordinationEventType
    node_id: str = Field(min_length=1)
    task_id: str | None = None
    assignment_id: str | None = None
    assignment_epoch: int | None = Field(default=None, ge=1)
    based_on_control_commit: str = Field(min_length=7)
    occurred_at: str
    payload: dict[str, Any] = Field(default_factory=dict)


def build_event_file_path(event_id: str) -> str:
    """返回事件文件在 ``maf/node/<node-id>`` 分支上的相对路径。

    格式：``.maf/events/<event_id>.json``（与《系统设计文档》§9 对齐：
    ``maf/node/<node-id>:.maf/events/<event-id>.json``）。每个事件一个独立
    文件，确保 append-only：追加新事件不会覆盖已有事件文件。
    """
    if not event_id:
        raise ValueError("event_id must not be empty")
    return f"{_EVENT_DIR_PREFIX}{event_id}.json"


def build_node_branch_name(node_id: str) -> str:
    """返回节点事件分支名 ``maf/node/<node_id>``。"""
    if not node_id:
        raise ValueError("node_id must not be empty")
    return f"{NODE_BRANCH_PREFIX}{node_id}"


# --------------------------------------------------------------------------- #
# TASK-024: Assignment Epoch Fencing 数据结构
# --------------------------------------------------------------------------- #

#: epoch 校验结果——通过（``reason_code`` 为 ``None``）。
EPOCH_CHECK_PASSED: str = "epoch_check_passed"

#: epoch 校验结果——事件 epoch 缺失（事件未携带 ``assignment_epoch``）。
EPOCH_CHECK_MISSING: str = "epoch_check_missing"

#: epoch 校验结果——事件 epoch 早于当前权威 epoch（旧 epoch，被 fencing 拒绝）。
EPOCH_CHECK_STALE: str = "epoch_check_stale"

#: epoch 校验结果——事件 epoch 晚于当前权威 epoch（未来 epoch，非法）。
EPOCH_CHECK_FUTURE: str = "epoch_check_future"

#: epoch 校验拒绝时写入 ``EventDecisionRepository.record_decision`` 的判定值。
#:
#: - ``skipped_stale_epoch``：旧 epoch 事件被 fencing 拒绝（不可重试，旧 epoch
#:   永远不会追上当前权威 epoch）；
#: - ``skipped_future_epoch``：未来 epoch 事件被拒绝（不可重试，需节点重新
#:   fetch control 获取正确 epoch）。
#:
#: 这两个取值不在 TASK-021 的 ``EVENT_DECISION_*`` 常量集合中，因为它们是
#: TASK-024 引入的新 fencing 判定。``record_decision`` 接受任意 ``str``
#: 作为 ``decision`` 参数；这些取值不在 ``_RETRYABLE_DECISIONS`` 集合中，
#: 因此 ``has_processed`` 会返回 ``True``（旧/未来 epoch 事件不应重试）。
EPOCH_DECISION_SKIPPED_STALE: str = "skipped_stale_epoch"
EPOCH_DECISION_SKIPPED_FUTURE: str = "skipped_future_epoch"


@dataclass(frozen=True)
class EpochCheckResult:
    """``assignment_epoch`` fencing 校验结果（TASK-024）。

    由 :func:`maf_server.modules.git_coordination.service.check_assignment_epoch`
    返回，描述事件 epoch 与当前权威 epoch 的比较结果。设计为不可变数据类，
    便于在日志、审计和 :class:`EventDecisionRepository.record_decision` 调用
    中稳定传递。

    字段：

    - ``task_id``：被校验的任务 ID（用于审计与 ``record_decision`` 的 ``result``）；
    - ``event_epoch``：事件携带的 ``assignment_epoch``（``None`` 表示事件未
      携带 epoch，会被拒绝）；
    - ``current_epoch``：当前权威 epoch（来自 SQLite 投影，由 TASK-027 维护）；
    - ``passed``：``True`` 当且仅当 ``event_epoch == current_epoch`` 且
      ``event_epoch`` 非 ``None``；
    - ``outcome``：稳定结果码，取 :data:`EPOCH_CHECK_PASSED` /
      :data:`EPOCH_CHECK_MISSING` / :data:`EPOCH_CHECK_STALE` /
      :data:`EPOCH_CHECK_FUTURE`；
    - ``reason_code``：``passed`` 时为 ``None``；拒绝时为对应的
      :class:`maf_domain.errors.ReasonCode` 字符串值（``EVENT_EPOCH_STALE``
      或 ``EVENT_SCHEMA_INVALID``），供 :class:`GitEventRejectedError` 使用；
    - ``decision``：``passed`` 时为 ``None``；拒绝时为对应的
      :data:`EPOCH_DECISION_SKIPPED_STALE` / :data:`EPOCH_DECISION_SKIPPED_FUTURE`，
      供 :meth:`EventDecisionRepository.record_decision` 作为 ``decision`` 参数；
    - ``message``：人类可读的校验结果说明（不含敏感信息）。

    设计决策：

    - **不可变**：``frozen=True`` 防止调用方误改结果；
    - **稳定字符串**：``outcome`` / ``decision`` 使用模块级常量，禁止自由文本，
      便于节点按确定原因重试或转人工（与 :class:`ReasonCode` 设计一致）；
    - **不抛异常**：:func:`check_assignment_epoch` 返回本结构而非抛异常，
      便于调用方在 ``process_event`` 流程中先记录判定再决定是否继续处理。
      需要抛异常的调用方使用
      :func:`maf_server.modules.git_coordination.service.validate_assignment_epoch`。
    """

    task_id: str
    event_epoch: int | None
    current_epoch: int
    passed: bool
    outcome: str
    reason_code: str | None
    decision: str | None
    message: str


# --------------------------------------------------------------------------- #
# TASK-025: 进度与阻塞事件处理结果
# --------------------------------------------------------------------------- #

#: ``ProcessResult.decision`` 取值——事件已成功应用（PROGRESS 更新进度，
#: BLOCKED 完成 IN_PROGRESS→BLOCKED 状态转换）。
PROCESS_DECISION_APPLIED: str = "applied"

#: ``ProcessResult.decision`` 取值——事件因重复（同 ``event_id`` 已处理过）被跳过。
PROCESS_DECISION_SKIPPED_DUPLICATE: str = "skipped_duplicate"

#: ``ProcessResult.decision`` 取值——事件因 ``assignment_epoch`` 旧于当前权威
#: epoch 被 fencing 拒绝（与 :data:`EPOCH_DECISION_SKIPPED_STALE` 同值）。
PROCESS_DECISION_SKIPPED_STALE: str = EPOCH_DECISION_SKIPPED_STALE

#: ``ProcessResult.decision`` 取值——事件因 ``assignment_epoch`` 缺失或晚于
#: 当前权威 epoch 被拒绝（与 :data:`EPOCH_DECISION_SKIPPED_FUTURE` 同值）。
PROCESS_DECISION_SKIPPED_FUTURE: str = EPOCH_DECISION_SKIPPED_FUTURE

#: ``ProcessResult.decision`` 取值——事件因 payload 缺失/非法或状态转换不合法
#: 处理失败（与 TASK-021 :data:`EVENT_DECISION_FAILED` 同值，可被后续重试覆盖）。
PROCESS_DECISION_FAILED: str = "failed"


@dataclass(frozen=True)
class ProcessResult:
    """``process_event(PROGRESS/BLOCKED)`` 的处理结果（TASK-025）。

    由 :meth:`maf_server.modules.git_coordination.service.LocalGitCoordinationService.process_event`
    返回，描述 PROGRESS_REPORTED / BLOCKED_REPORTED 事件的处理结果。设计为不可变
    数据类，便于在日志、审计和 :meth:`EventDecisionRepository.record_decision`
    调用中稳定传递。

    字段：

    - ``event_id``：被处理事件的 ID（与 :class:`CoordinationEventModel.event_id`
      一致），便于调用方关联事件与处理结果；
    - ``processed``：``True`` 表示事件被成功应用（PROGRESS 更新进度，BLOCKED
      完成 IN_PROGRESS→BLOCKED 状态转换）；``False`` 表示事件被跳过（重复 /
      epoch fencing 拒绝）或处理失败；
    - ``new_state``：处理后的任务状态。PROGRESS 不改变状态，``new_state=None``；
      BLOCKED 通过 :class:`TaskStateMachine.transition` 完成 IN_PROGRESS→BLOCKED
      转换，``new_state=TaskState.BLOCKED``。被跳过/失败的事件 ``new_state=None``；
    - ``decision``：稳定判定值，取 :data:`PROCESS_DECISION_APPLIED` /
      :data:`PROCESS_DECISION_SKIPPED_DUPLICATE` /
      :data:`PROCESS_DECISION_SKIPPED_STALE` /
      :data:`PROCESS_DECISION_SKIPPED_FUTURE` / :data:`PROCESS_DECISION_FAILED`，
      与 :meth:`EventDecisionRepository.record_decision` 的 ``decision`` 参数对齐；
    - ``error``：失败/拒绝时的错误信息（来自 :class:`EpochCheckResult.message`
      或异常 ``str``），成功时为 ``None``；
    - ``reason_code``：失败/拒绝时的稳定 reason code（来自
      :class:`ReasonCode` 字符串值），成功时为 ``None``。

    设计决策：

    - **不可变**：``frozen=True`` 防止调用方误改结果；
    - **稳定字符串**：``decision`` 使用模块级常量，禁止自由文本，便于节点按
      确定原因重试或转人工（与 :class:`ReasonCode` 设计一致）；
    - **不抛异常的返回值**：成功/跳过路径返回 :class:`ProcessResult`；
      非法事件（payload 缺失/状态转换不合法）在记录 ``failed`` 判定后由
      ``process_event`` 重新抛出异常（与 :func:`process_event_idempotently`
      一致），调用方可在事务内捕获并提交以持久化 ``failed`` 跟踪记录。
    - **epoch fencing 决策复用**：``PROCESS_DECISION_SKIPPED_STALE`` /
      ``PROCESS_DECISION_SKIPPED_FUTURE`` 直接复用 TASK-024 的
      :data:`EPOCH_DECISION_SKIPPED_STALE` / :data:`EPOCH_DECISION_SKIPPED_FUTURE`，
      保证 fencing 拒绝判定跨任务稳定一致。
    """

    event_id: str
    processed: bool
    new_state: str | None
    decision: str
    error: str | None
    reason_code: str | None
