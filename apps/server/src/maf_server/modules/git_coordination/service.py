"""Git 单写协调、节点事件消费和 SQLite 投影接口。

TASK-022 阶段提供 ``TaskStateMachine`` 的服务层包装 ``GitCoordinationStateService``，
仅完成任务状态转换相关方法；TASK-015 在本模块新增 ``LocalGitCoordinationService``，
实现 ``GitCoordinationService.initialize_project``：在仓库创建或验证
``maf/control`` 分支并写入 ``.maf/`` 协议目录。``publish_tasks``、``process_event``、
``sync`` 等 Git 协调方法由 TASK-016 至 TASK-021 填充。

TASK-016 在本模块新增 ``CoordinationSnapshot`` 数据结构与
``LocalGitCoordinationService.fetch_control``，只读访问 ``maf/control`` 分支
当前快照（commit、project.yaml、status.md、tasks/nodes/events 目录列表），
供中央调度器与节点端 ``RunnerGitClient.fetch_control`` 复用。

TASK-023 在本模块新增 ``TaskAllocator`` 类与 ``ClaimDecision`` 数据结构：
从 :class:`CoordinationSnapshot` 中为节点确定性地选择 READY 任务（能力匹配、
优先级、task_id 字典序 tiebreaker、排除节点已处理任务），并返回单调递增的
``assignment_epoch``（供 TASK-024 fencing 校验）。分配逻辑是同步、纯内存计算，
不涉及 Git/IO、随机数、时间戳或 LLM。

TASK-024 在本模块新增 ``AssignmentEpochStaleError``、``AssignmentEpochError``
异常与 :func:`check_assignment_epoch` / :func:`validate_assignment_epoch` 校验
函数，实现 Assignment Epoch Fencing：防止旧节点用过期 epoch 覆盖新结果
（《GitHub 分布式协作协议》§7）。``check_assignment_epoch`` 返回
:class:`EpochCheckResult`（不抛异常），供 ``process_event`` 在处理节点事件前
先校验 epoch 并记录判定；``validate_assignment_epoch`` 是其抛异常版本。

TASK-025 在 :class:`LocalGitCoordinationService` 新增 :meth:`process_event`
方法（PROGRESS/BLOCKED），编排"幂等校验（TASK-021）→ epoch fencing
（TASK-024）→ 事件分发 → 记录判定"流程：

- ``PROGRESS_REPORTED``：校验 ``payload.progress_percent`` ∈ [0, 100]，
  不改变任务状态（仍 ``IN_PROGRESS``），不更新 SQLite 投影（由 TASK-027 维护）；
- ``BLOCKED_REPORTED``：校验 ``payload.block_reason`` 非空，通过
  :meth:`GitCoordinationStateService.apply_task_event` 完成
  ``IN_PROGRESS → BLOCKED`` 状态转换；
- 重复事件（同 ``event_id`` 已处理）记录 ``skipped_duplicate`` 跳过；
- 旧/未来 epoch 事件记录 ``skipped_stale_epoch`` / ``skipped_future_epoch`` 跳过；
- 非法 payload / 状态转换不合法记录 ``failed`` 后抛异常。

返回 :class:`ProcessResult`（``event_id`` / ``processed`` / ``new_state`` /
``decision`` / ``error`` / ``reason_code``），调用方据此推进 SQLite 投影与
``sync`` 循环。本任务范围不含 SUBMISSION/DONE（由 TASK-026 处理）和 UI 展示。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypedDict, cast

import structlog
import yaml

from maf_artifact_schemas.protocol import ProtocolVersion, SchemaRef
from maf_contracts.coordination import (
    NODE_BRANCH_PREFIX,
    CoordinationEvent,
    CoordinationEventModel,
    CoordinationTask,
    EPOCH_CHECK_FUTURE,
    EPOCH_CHECK_MISSING,
    EPOCH_CHECK_PASSED,
    EPOCH_CHECK_STALE,
    EPOCH_DECISION_SKIPPED_FUTURE,
    EPOCH_DECISION_SKIPPED_STALE,
    EpochCheckResult,
    EventDecision,
    NodeManifest,
    PROCESS_DECISION_APPLIED,
    PROCESS_DECISION_FAILED,
    PROCESS_DECISION_SKIPPED_DUPLICATE,
    PROCESS_DECISION_SKIPPED_FUTURE,
    PROCESS_DECISION_SKIPPED_STALE,
    ProcessResult,
    build_event_file_path,
    build_node_branch_name,
)
from maf_domain.errors import (
    ArgumentError,
    ErrorCode,
    ExternalDependencyError,
    GitEventRejectedError,
    ReasonCode,
    UnsupportedOperationError,
    ValidationError,
)
from maf_domain.states import (
    Actor,
    TaskEvent,
    TaskState,
    TaskStateMachine,
    TaskStateTransition,
)

from .repository import compute_event_content_hash
from .schemas import SyncResult
from ...core.events import (
    EventConsumer,
    has_processed_event,
    record_event_decision,
)
from ...core.security import (
    extract_node_identity_from_manifest,
    verify_commit_author,
)
from ...git_coordination.schemas import SchemaLoader, YamlLoader


# --------------------------------------------------------------------------- #
# TASK-016: CoordinationSnapshot 数据结构
# --------------------------------------------------------------------------- #


class CoordinationSnapshot(TypedDict):
    """``maf/control`` 分支当前快照（TASK-016，只读）。

    包含 control 分支当前的关键信息：

    - ``project_id``：从 ``.maf/project.yaml`` 解析得到的项目 ID；
    - ``control_commit``：``maf/control`` 分支当前 HEAD commit hash（40 字符 SHA-1），
      调用方据此判断"无变化"避免重复处理；
    - ``commit_timestamp``：control HEAD commit 的提交时间（ISO 8601，UTC）；
    - ``project_yaml``：``.maf/project.yaml`` 解析后的字典；
    - ``status_md``：``.maf/status.md`` 的原始文本内容；
    - ``tasks_paths``：``.maf/tasks/`` 下所有文件路径（相对仓库根，含 ``.maf/tasks/`` 前缀）；
    - ``nodes_paths``：``.maf/nodes/`` 下所有文件路径；
    - ``events_paths``：``.maf/events/`` 下所有文件路径；
    - ``tasks``：从 ``tasks/`` 解析的任务对象列表（TASK-016 仅提供空列表占位，
      解析由 TASK-017/019 等后续任务填充）；
    - ``nodes``：从 ``nodes/`` 解析的节点对象列表（同上，仅占位）；
    - ``generated_at``：快照生成时间（ISO 8601，UTC），便于审计。

    本 TypedDict 是 :class:`maf_contracts.coordination.CoordinationSnapshot` 的超集
    （包含其全部字段并扩展），可直接传给 ``GitCoordinationRepository.project_snapshot``。
    本任务范围禁止修改 ``contracts_py``，故在此文件内单独定义。

    读取保证：

    - **只读**：所有 git 命令为 ``show`` / ``ls-tree`` / ``log`` / ``rev-parse``，
      不修改工作区、不切换分支、不 push；
    - **原子性**：读取过程中任一文件缺失或 Schema 错误时抛异常，不返回部分快照；
    - **去重**：相同 commit 的重复调用可通过 ``control_commit`` 识别。
    """

    project_id: str
    control_commit: str
    commit_timestamp: str
    project_yaml: dict[str, Any]
    status_md: str
    tasks_paths: list[str]
    nodes_paths: list[str]
    events_paths: list[str]
    tasks: list[CoordinationTask]
    nodes: list[NodeManifest]
    generated_at: str


# --------------------------------------------------------------------------- #
# TASK-019: Event discovery result types
# --------------------------------------------------------------------------- #


class InvalidEventEntry(TypedDict):
    """An event file that failed Schema validation (reported, not silently dropped).

    - ``path``: file path on the node branch (e.g. ``.maf/events/<event-id>.json``);
    - ``error``: validation/parse error message;
    - ``raw_content``: raw file content when readable (for debugging), ``None``
      when the file could not be read at all.
    """

    path: str
    error: str
    raw_content: str | None


class DiscoveredEvents(TypedDict):
    """Result of discovering events on a single node branch.

    - ``node_id``: the node whose branch was scanned;
    - ``branch``: ``maf/node/<node-id>``;
    - ``branch_exists``: whether the node branch exists locally;
    - ``latest_commit``: current HEAD of the node branch (``None`` when branch
      does not exist); callers use this as the next ``since_commit`` watermark;
    - ``events``: valid events sorted by ``event_id`` (deterministic, not
      machine time);
    - ``invalid_events``: files that failed validation, sorted by ``path``;
    - ``diverged``: ``True`` when ``since_commit`` is not an ancestor of the
      current HEAD (force-push / history rollback); callers should isolate the
      branch and fall back to full scan;
    - ``scanned_paths``: all event file paths that were read during this call.
    """

    node_id: str
    branch: str
    branch_exists: bool
    latest_commit: str | None
    events: list[CoordinationEvent]
    invalid_events: list[InvalidEventEntry]
    diverged: bool
    scanned_paths: list[str]


# --------------------------------------------------------------------------- #
# TASK-020: 节点身份验证结果与错误
# --------------------------------------------------------------------------- #


class NodeIdentity(TypedDict):
    """节点身份验证结果（TASK-020）。

    - ``node_id``：被验证的节点 ID；
    - ``verified``：是否通过验证（``True``；失败时抛 :class:`NodeIdentityError`，
      不会返回 ``verified=False`` 的结果）；
    - ``verification_method``：验证方法（MVP 为 ``"commit_author_email"``，
      完整 GPG/SSH 签名验证由后续任务增强）；
    - ``commit_author``：事件 commit 的作者 ``{"name": ..., "email": ...}``，
      来自 ``git log -1 --format=%an%n%ae <branch> -- <event-path>``；
    - ``manifest``：验证所用的节点清单（来自 control 分支 ``nodes/<node-id>.yaml``
      或 ``NODE_REGISTERED`` 事件 ``payload.manifest``）；
    - ``failure_reason``：失败原因（成功时为空字符串；失败时由
      :class:`NodeIdentityError.context["failure_reason"]` 携带）。
    """

    node_id: str
    verified: bool
    verification_method: str
    commit_author: dict[str, str]
    manifest: NodeManifest | None
    failure_reason: str


class NodeIdentityError(GitEventRejectedError):
    """节点身份验证失败（TASK-020）。

    验证事件 commit author / source node_id 与已注册节点 manifest 不一致，
    或事件来自未注册节点时抛出。``reason_code`` 必须取自
    ``ReasonCode.EVENT_NODE_UNKNOWN`` 或 ``ReasonCode.EVENT_NODE_IDENTITY_MISMATCH``，
    禁止自由文本，以便节点按确定原因重试或转人工（对应 TASK-020 验收
    "拒绝原因写审计且不泄露凭据"）。

    ``context`` 必含 ``node_id`` 和 ``failure_reason``，便于审计；不含
    commit author email 以外的凭据（email 不是凭据但保留用于排查）。
    """

    error_code = ErrorCode.GIT_EVENT_REJECTED
    default_retryable = False


# --------------------------------------------------------------------------- #
# TASK-024: Assignment Epoch Fencing 异常与校验函数
# --------------------------------------------------------------------------- #


class AssignmentEpochStaleError(GitEventRejectedError):
    """旧 ``assignment_epoch`` 事件被 fencing 拒绝（TASK-024）。

    当 ``event.assignment_epoch < current_epoch`` 时抛出：节点持有的 epoch 已
    过期（任务被重新分配给其他节点并递增了 epoch），旧节点提交的进度/阻塞/
    提交事件不能覆盖新权威状态（《GitHub 分布式协作协议》§7）。

    ``reason_code`` 固定为 :data:`ReasonCode.EVENT_EPOCH_STALE`；``context`` 必含
    ``task_id``、``event_epoch``、``current_epoch``，便于审计与节点重试决策。
    旧 epoch 事件不可重试（``retryable=False``）：旧 epoch 永远不会追上当前
    权威 epoch，节点必须重新 fetch control 确认是否仍持有任务。
    """

    error_code = ErrorCode.GIT_EVENT_REJECTED
    default_retryable = False

    def __init__(
        self,
        message: str,
        *,
        context: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(
            message,
            reason_code=ReasonCode.EVENT_EPOCH_STALE,
            context=context,
            retryable=retryable,
        )


class AssignmentEpochError(GitEventRejectedError):
    """未来 ``assignment_epoch`` 事件被拒绝（TASK-024）。

    当 ``event.assignment_epoch > current_epoch`` 时抛出：事件携带的 epoch
    超过当前权威 epoch，属于非法值（中央调度器尚未分配该 epoch）。可能原因：
    节点本地状态损坏、并发写冲突或恶意事件。``reason_code`` 使用
    :data:`ReasonCode.EVENT_SCHEMA_INVALID`（未来 epoch 是非法事件值）。

    ``context`` 必含 ``task_id``、``event_epoch``、``current_epoch``。
    不可重试（``retryable=False``）：节点必须重新 fetch control 获取正确 epoch。
    """

    error_code = ErrorCode.GIT_EVENT_REJECTED
    default_retryable = False

    def __init__(
        self,
        message: str,
        *,
        context: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(
            message,
            reason_code=ReasonCode.EVENT_SCHEMA_INVALID,
            context=context,
            retryable=retryable,
        )


def check_assignment_epoch(
    task_id: str,
    event_epoch: int | None,
    *,
    current_epoch: int,
) -> EpochCheckResult:
    """校验事件 epoch 与当前权威 epoch，返回 :class:`EpochCheckResult`（不抛异常）。

    这是 TASK-024 fencing 校验的核心函数。``process_event``（TASK-025/026）
    在处理节点事件（PROGRESS/BLOCKED/SUBMISSION_CREATED 等）前应先调用本函数；
    返回 ``passed=False`` 时，调用方应：

    1. 通过 :meth:`EventDecisionRepository.record_decision` 记录判定，``decision``
       取 ``result.decision``（``skipped_stale_epoch`` 或 ``skipped_future_epoch``），
       ``result`` 取 ``f"task_id={task_id}"``，``error`` 取 ``result.message``；
    2. 跳过该事件的处理（不更新任务状态、不 push control）；
    3. 保留旧任务分支（不删除，作为可选恢复材料，对应 TASK-024 验收
       "旧任务分支不会被删除"）。

    fencing 逻辑（与《GitHub 分布式协作协议》§7 对齐）：

    - ``event_epoch is None`` → 拒绝（``EPOCH_CHECK_MISSING``）：事件未携带
      epoch，无法 fencing 校验；
    - ``event_epoch < current_epoch`` → 拒绝（``EPOCH_CHECK_STALE``）：旧 epoch
      被 fencing，防止旧节点覆盖新结果；
    - ``event_epoch > current_epoch`` → 拒绝（``EPOCH_CHECK_FUTURE``）：未来
      epoch 非法，中央尚未分配该 epoch；
    - ``event_epoch == current_epoch`` → 通过（``EPOCH_CHECK_PASSED``）。

    参数：
        task_id: 被校验的任务 ID（非空，用于审计）。
        event_epoch: 事件携带的 ``assignment_epoch``（``None`` 表示缺失）。
        current_epoch: 当前权威 epoch（来自 SQLite 投影，由 TASK-027 维护；
            必须 >= 1，与 :class:`CoordinationEventModel.assignment_epoch` 的
            ``ge=1`` 约束一致）。

    返回：:class:`EpochCheckResult`，``passed=True`` 表示通过，
        ``passed=False`` 表示被拒绝（含 ``reason_code`` 与 ``decision``）。

    异常：
        ArgumentError: ``task_id`` 为空，或 ``current_epoch < 1``。

    设计决策：

    - **不抛 fencing 异常**：返回结构化结果而非抛异常，便于调用方先记录判定
      再决定后续流程（避免在 ``record_decision`` 前 unwrap 异常丢失上下文）；
      需要抛异常的调用方使用 :func:`validate_assignment_epoch`。
    - **单调递增整数 epoch**：严格遵守协议"epoch 是分布式 fencing token，
      不能用时间戳替代"。``current_epoch`` 是整数，比较是算术比较。
    - **不读 SQLite**：``current_epoch`` 由调用方传入（TASK-027 维护投影），
      本函数是纯内存计算，便于单元测试。
    """
    if not task_id:
        raise ArgumentError("task_id must not be empty")
    if current_epoch < 1:
        raise ArgumentError(
            "current_epoch must be >= 1",
            context={"current_epoch": current_epoch},
        )

    if event_epoch is None:
        return EpochCheckResult(
            task_id=task_id,
            event_epoch=None,
            current_epoch=current_epoch,
            passed=False,
            outcome=EPOCH_CHECK_MISSING,
            reason_code=ReasonCode.EVENT_SCHEMA_INVALID.value,
            decision=EPOCH_DECISION_SKIPPED_FUTURE,
            message=(
                f"event for task {task_id!r} has no assignment_epoch; "
                f"current epoch is {current_epoch}"
            ),
        )

    if event_epoch < current_epoch:
        return EpochCheckResult(
            task_id=task_id,
            event_epoch=event_epoch,
            current_epoch=current_epoch,
            passed=False,
            outcome=EPOCH_CHECK_STALE,
            reason_code=ReasonCode.EVENT_EPOCH_STALE.value,
            decision=EPOCH_DECISION_SKIPPED_STALE,
            message=(
                f"stale assignment_epoch {event_epoch} for task {task_id!r}; "
                f"current epoch is {current_epoch}"
            ),
        )

    if event_epoch > current_epoch:
        return EpochCheckResult(
            task_id=task_id,
            event_epoch=event_epoch,
            current_epoch=current_epoch,
            passed=False,
            outcome=EPOCH_CHECK_FUTURE,
            reason_code=ReasonCode.EVENT_SCHEMA_INVALID.value,
            decision=EPOCH_DECISION_SKIPPED_FUTURE,
            message=(
                f"future assignment_epoch {event_epoch} for task {task_id!r}; "
                f"current epoch is {current_epoch}"
            ),
        )

    return EpochCheckResult(
        task_id=task_id,
        event_epoch=event_epoch,
        current_epoch=current_epoch,
        passed=True,
        outcome=EPOCH_CHECK_PASSED,
        reason_code=None,
        decision=None,
        message=(
            f"assignment_epoch {event_epoch} matches current epoch "
            f"for task {task_id!r}"
        ),
    )


def validate_assignment_epoch(
    event: CoordinationEventModel,
    current_epoch: int,
    *,
    task_id: str,
) -> None:
    """校验事件的 ``assignment_epoch`` 与当前权威 epoch 一致，失败时抛异常。

    本函数是 :func:`check_assignment_epoch` 的抛异常版本，供需要在 fencing
    失败时直接中断流程的调用方使用（如 ``process_event`` 的早期校验阶段）。
    内部调用 :func:`check_assignment_epoch`，根据结果抛出对应的异常：

    - ``EPOCH_CHECK_MISSING`` / ``EPOCH_CHECK_FUTURE`` →
      :class:`AssignmentEpochError`（未来/缺失 epoch 非法）；
    - ``EPOCH_CHECK_STALE`` → :class:`AssignmentEpochStaleError`（旧 epoch 被
      fencing）；
    - ``EPOCH_CHECK_PASSED`` → 正常返回（无异常）。

    参数：
        event: 待校验的协调事件（已通过 :class:`CoordinationEventModel` 校验）。
            本函数读取 ``event.assignment_epoch`` 和 ``event.task_id``。
        current_epoch: 当前权威 epoch（>= 1，来自 SQLite 投影）。
        task_id: 被校验的任务 ID（非空）。当 ``event.task_id`` 非 ``None``
            且与 ``task_id`` 不一致时仍以本参数为准（调用方已确定任务上下文）。

    异常：
        ArgumentError: ``task_id`` 为空或 ``current_epoch < 1``。
        AssignmentEpochStaleError: ``event.assignment_epoch < current_epoch``。
        AssignmentEpochError: ``event.assignment_epoch`` 为 ``None`` 或
            ``> current_epoch``。
    """
    result = check_assignment_epoch(
        task_id,
        event.assignment_epoch,
        current_epoch=current_epoch,
    )
    if result.passed:
        return

    context: dict[str, Any] = {
        "task_id": task_id,
        "event_epoch": event.assignment_epoch,
        "current_epoch": current_epoch,
        "event_id": event.event_id,
        "outcome": result.outcome,
    }

    if result.outcome == EPOCH_CHECK_STALE:
        raise AssignmentEpochStaleError(
            result.message,
            context=context,
        )

    # EPOCH_CHECK_MISSING 或 EPOCH_CHECK_FUTURE → AssignmentEpochError
    raise AssignmentEpochError(
        result.message,
        context=context,
    )


class GitCoordinationService(Protocol):
    async def initialize_project(self, repository_binding_id: str, project_id: str) -> str:
        """在仓库创建或验证 `maf/control` 与 `.maf` 协议文件，返回初始 control commit。

        只允许在空协议或兼容版本上初始化；已有不兼容协议必须停止。中央调度器是唯一写者，
        初始化不得修改 main 上的业务代码。
        """
        ...

    async def fetch_control(self, project_id: str) -> CoordinationSnapshot:
        """只读访问 ``maf/control`` 分支当前快照，返回 :class:`CoordinationSnapshot`。

        使用 ``git show`` / ``git ls-tree`` / ``git log`` 等只读命令读取 control 分支
        的 project.yaml、status.md、tasks/nodes/events 目录列表和最新 commit 信息。

        - **只读**：绝不修改或推送 control 分支，不切换工作树分支；
        - **原子性**：任一关键文件缺失或 Schema 错误时抛异常，不返回部分快照；
        - **去重**：snapshot 携带 ``control_commit``，调用方可据此跳过相同 commit 的重复处理。

        参数：
            project_id: 期望的项目 ID（用于校验 ``.maf/project.yaml`` 一致性）。

        异常：
            ArgumentError: ``project_id`` 为空，或与 control 上 ``.maf/project.yaml`` 不一致。
            UnsupportedOperationError: ``maf/control`` 分支不存在或协议不兼容。
            ExternalDependencyError: git 只读命令执行失败。
            ValidationError: ``.maf/project.yaml`` 不符合 ``project-v1`` Schema。
        """
        ...

    async def publish_tasks(self, project_id: str, tasks: list[CoordinationTask], expected_control_commit: str) -> str:
        """校验依赖、ID、Schema 和 expected head 后写入独立 task 文件并生成 status.md。

        push 使用 fast-forward；远端 head 不匹配时重新 fetch/reconcile，禁止 force push。成功返回
        新 control commit，确认远端可见后才能推进 SQLite 投影水位。
        """
        ...

    async def register_node_event(self, event: CoordinationEvent) -> EventDecision:
        """验证 NODE_REGISTERED/NODE_UPDATED 事件的分支所有者、签名、能力和 Schema。

        接受后由中央调度器写 nodes/<node-id>.yaml；节点不能直接写 control。重复 event_id 返回
        首次决定。
        """
        ...

    async def process_event(self, event: CoordinationEvent) -> EventDecision:
        """处理认领、进度、阻塞、提交或放弃事件。

        依次校验事件分支/签名、event_id、based_on_control_commit、节点状态、任务当前状态、
        assignment_id/epoch 和允许的状态转换；接受后更新 task/event/status 并 push control。
        旧 epoch 事件只能记录为拒绝，不能覆盖当前任务。
        """
        ...

    async def sync(self, project_id: str) -> SyncResult:
        """fetch control 和全部 `maf/node/*`，按确定顺序处理未消费事件并更新 SQLite 投影。

        节点分支仅接受 fast-forward 历史；事件按 occurred_at 只用于显示，处理顺序使用 Git 可达
        顺序和 event_id。单次失败保留 projector 水位供重放。
        """
        ...

    async def reconcile_expired_assignments(self, project_id: str, now: str) -> list[str]:
        """检查长时间无有效进度的 ASSIGNED/IN_PROGRESS 任务。

        先 fetch 任务分支确认是否有未报告提交，再经过宽限期；失效时写 LEASE_EXPIRED、清空
        owner，并在重新分配时递增 assignment_epoch。返回发生变化的 task IDs。
        """
        ...

    async def rebuild_projection(self, project_id: str) -> str:
        """从 control 当前任务/节点和 canonical events 重建 SQLite，返回投影水位 commit。"""
        ...


class ClaimDecision(TypedDict):
    """``TaskAllocator.choose_claim`` 的返回值（TASK-023）。

    - ``task_id``：被分配的任务 ID；无可用任务时为 ``None``；
    - ``node_id``：发起 claim 的节点 ID；
    - ``reason``：分配决定原因（``claim_granted`` 或 ``no_matching_tasks``）；
    - ``assignment_epoch``：单调递增的分配 epoch，用于 TASK-024 fencing；
      无分配时为 ``None``。

    本结构与 :class:`EventDecision` 互补：``EventDecision`` 描述节点事件被
    接受/拒绝，``ClaimDecision`` 描述中央调度器为节点选定的任务分配结果。
    ``reason`` 取稳定字符串而非自由文本，便于节点按确定原因重试或转人工。
    """

    task_id: str | None
    node_id: str
    reason: str
    assignment_epoch: int | None


class TaskAllocator:
    """确定性任务分配器（TASK-023）。

    为发起 claim 的节点从 control 快照中选择最适合的 READY 任务：

    - **确定性**：相同输入产生相同输出（task 选择与 epoch 都依赖确定排序与
      实例状态，不使用随机数、时间戳或 LLM）；
    - **能力匹配**：任务 ``requirements.required_capabilities`` 必须是节点
      ``capabilities`` 的子集，否则任务被排除；
    - **优先级排序**：``priority`` 数值大的优先（高优先级先分配）；
    - **字典序 tiebreaker**：同优先级按 ``task_id`` 字典序升序（确定性）；
    - **排除已处理**：节点已是 ``assignment.node_id`` 的任务被排除，
      避免重复认领（含 lease 过期后 requeue 但 assignment 字段未清的边界）。

    ``assignment_epoch`` 是单调递增的整数，每次成功分配递增；TASK-024 用此值
    做 fencing，旧 epoch 事件不能覆盖新 epoch 的权威状态。

    设计决策：

    - **同步方法**：``choose_claim`` 是纯内存计算，不涉及 Git/IO，故同步而非
      ``async``，便于在调度器内直接组合；
    - **epoch 起始值**：默认 ``initial_epoch=0``，首次分配返回 ``1``（1-based，
      与 :class:`maf_contracts.coordination.CoordinationEventModel.assignment_epoch``
      的 ``ge=1`` 约束一致）；
    - **不修改 snapshot**：``choose_claim`` 是只读操作，调用方负责持久化分配
      结果到 control 分支（由 ``process_event`` / ``publish_tasks`` 完成）。
    """

    #: 默认初始 epoch（首次分配返回 1）。
    _DEFAULT_INITIAL_EPOCH: int = 0

    #: 成功分配的 reason 取值（与 :class:`ClaimDecision.reason` 对齐）。
    REASON_CLAIM_GRANTED: str = "claim_granted"

    #: 无可用任务的 reason 取值（与 :class:`ClaimDecision.reason` 对齐）。
    REASON_NO_MATCHING_TASKS: str = "no_matching_tasks"

    def __init__(self, *, initial_epoch: int = _DEFAULT_INITIAL_EPOCH) -> None:
        if initial_epoch < 0:
            raise ArgumentError(
                "initial_epoch must be non-negative",
                context={"initial_epoch": initial_epoch},
            )
        self._next_epoch: int = initial_epoch

    @property
    def next_assignment_epoch(self) -> int:
        """下一次成功分配将返回的 epoch 值（只读，便于测试断言）。"""
        return self._next_epoch + 1

    def choose_claim(
        self,
        snapshot: CoordinationSnapshot,
        node_id: str,
        node_capabilities: NodeManifest,
    ) -> ClaimDecision:
        """为 ``node_id`` 确定性地选择一个 READY 任务。

        选择流程（全部确定性，无随机/LLM/时间戳）：

        1. 收集节点已经在处理的 task_id（``assignment.node_id == node_id``，
           不论任务状态）。这覆盖 lease 过期后 requeue 但 assignment 字段
           未清的边界，避免把同一任务重复派给同节点；
        2. 筛选 ``status == READY`` 且 ``assignment is None`` 且节点未处理
           的任务作为候选；
        3. 能力匹配：``task.requirements.required_capabilities`` 必须是
           ``node_capabilities.capabilities`` 的子集；
        4. 确定性排序：``priority`` 降序（高优先级先），``task_id`` 升序
           （字典序 tiebreaker）；
        5. 取首位任务，递增 ``assignment_epoch`` 并返回 :class:`ClaimDecision`；
           无匹配任务返回 ``task_id=None``、``reason="no_matching_tasks"``。

        参数：
            snapshot: ``maf/control`` 当前快照（``tasks`` 字段含全部任务）；
            node_id: 发起 claim 的节点 ID；
            node_capabilities: 节点清单（含 ``capabilities`` 列表）。

        返回：:class:`ClaimDecision`，``task_id`` 为选中的任务 ID 与递增后的
            ``assignment_epoch``；无可用任务时 ``task_id=None``、
            ``reason="no_matching_tasks"``、``assignment_epoch=None``。

        异常：
            ArgumentError: ``node_id`` 为空。
        """
        if not node_id:
            raise ArgumentError("node_id must not be empty")

        node_caps = set(node_capabilities.get("capabilities", []) or [])

        # 1. 收集节点已经在处理的 task_id（assignment.node_id == node_id），
        #    不论任务状态。覆盖 lease 过期后 requeue 但 assignment 字段未清
        #    的边界情况，避免把同一任务重复派给同节点。
        node_active_task_ids: set[str] = set()
        for task in snapshot.get("tasks", []) or []:
            assignment = task.get("assignment")
            if assignment is not None and assignment.get("node_id") == node_id:
                task_id = task.get("task_id", "")
                if task_id:
                    node_active_task_ids.add(task_id)

        # 2. 筛选 READY + 未分配 + 节点未处理的任务。
        candidates: list[CoordinationTask] = []
        for task in snapshot.get("tasks", []) or []:
            if task.get("status") != "READY":
                continue
            assignment = task.get("assignment")
            if assignment is not None:
                # READY 状态通常 assignment 为 None；若残留 assignment 视为未真正 READY。
                continue
            task_id = task.get("task_id", "")
            if task_id in node_active_task_ids:
                continue
            candidates.append(task)

        # 3. 能力匹配：task.required_capabilities ⊆ node.capabilities。
        matching: list[CoordinationTask] = []
        for task in candidates:
            requirements = task.get("requirements") or {}
            required_caps = set(requirements.get("required_capabilities", []) or [])
            if required_caps.issubset(node_caps):
                matching.append(task)

        if not matching:
            return ClaimDecision(
                task_id=None,
                node_id=node_id,
                reason=self.REASON_NO_MATCHING_TASKS,
                assignment_epoch=None,
            )

        # 4. 确定性排序：priority 降序（高优先级先），task_id 升序（字典序）。
        matching.sort(
            key=lambda t: (-int(t.get("priority", 0) or 0), t.get("task_id", ""))
        )

        chosen = matching[0]
        # 5. 分配 epoch 递增（仅成功分配时递增，无匹配时不递增）。
        self._next_epoch += 1
        epoch = self._next_epoch

        return ClaimDecision(
            task_id=chosen["task_id"],
            node_id=node_id,
            reason=self.REASON_CLAIM_GRANTED,
            assignment_epoch=epoch,
        )


class EventDiscoveryService(Protocol):
    """Read-only discovery of node events on ``maf/node/<node-id>`` branches.

    Who calls it: the central scheduler ``sync`` loop (TASK-062) and the SQLite
    projector (TASK-027) call these methods to find new events written by nodes
    without modifying any branch.

    Design guarantees:

    - **Read-only**: only ``git ls-tree`` / ``git show`` / ``git log`` /
      ``git rev-list`` / ``git for-each-ref``; never modifies or pushes a
      node branch.
    - **Incremental**: ``since_commit`` allows callers to fetch only events
      appended after the last processed watermark, avoiding re-scanning the
      entire branch.
    - **Deterministic ordering**: events are sorted by ``event_id`` (not
      machine wall-clock ``occurred_at``) so replay is stable across nodes.
    - **Invalid events reported, not dropped**: files that fail
      :class:`CoordinationEventModel` validation are returned in
      ``invalid_events`` for caller-side quarantine; they are never silently
      discarded.
    - **Force-push / rollback isolation**: when ``since_commit`` is not an
      ancestor of the current HEAD (``diverged=True``), the caller is expected
      to isolate the branch and fall back to full scan.
    """

    async def discover_node_events(
        self,
        project_id: str,
        node_id: str,
        *,
        since_commit: str | None = None,
    ) -> DiscoveredEvents:
        """Discover new events on a single node's ``maf/node/<node-id>`` branch.

        - ``since_commit=None``: full scan of all ``.maf/events/*.json`` files.
        - ``since_commit=<commit>``: only return events added after that commit
          (fast-forward path); if the commit is not an ancestor of the current
          HEAD (force-push / rollback), ``diverged=True`` is returned and a full
          scan is performed so no events are missed.

        Events are validated with :class:`CoordinationEventModel`; invalid files
        appear in ``invalid_events``. The result ``latest_commit`` should be
        persisted as the next watermark.
        """
        ...

    async def discover_all_node_events(
        self,
        project_id: str,
        *,
        since_commit: str | None = None,
    ) -> dict[str, DiscoveredEvents]:
        """Discover new events across **all** node branches.

        Enumerates every local ``maf/node/*`` branch via ``git for-each-ref``
        and calls :meth:`discover_node_events` for each. Returns a dict keyed by
        ``node_id``. ``since_commit`` (when provided) is applied to every node
        branch as the watermark; callers needing per-node watermarks should call
        :meth:`discover_node_events` individually.
        """
        ...


class GitCoordinationStateService:
    """``TaskStateMachine`` 的服务层包装（TASK-022）。

    本类是 ``GitCoordinationService`` 的部分实现，仅完成任务状态转换相关方法。
    ``initialize_project``、``publish_tasks``、``process_event``、``sync`` 等
    Git 协调方法由 TASK-015 至 TASK-021 填充，本类不涉及 Git push、节点 fetch
    或 SQLite 投影。

    调用方（中央调度器、单元测试）通过本类校验状态转换合法性并取得递增后的
    任务版本号；非法转换或非授权 ``actor`` 抛出 ``UnsupportedOperationError``，
    ``context`` 包含 ``current_state``、``event``、``actor`` 与（若可确定）
    ``target_state`` 便于审计与重试决策。
    """

    def __init__(self, state_machine: TaskStateMachine | None = None) -> None:
        self._state_machine: TaskStateMachine = state_machine or TaskStateMachine()

    def apply_task_event(
        self,
        current_state: TaskState,
        event: TaskEvent,
        *,
        actor: Actor = Actor.SCHEDULER,
        current_version: int = 1,
    ) -> TaskStateTransition:
        """应用 ``TaskEvent``，返回新状态和递增后的版本号。

        成功返回 ``TaskStateTransition(new_state, current_version + 1)``；
        非法转换或非授权 ``actor`` 抛 ``UnsupportedOperationError``。
        """
        return self._state_machine.transition(
            current_state,
            event,
            actor=actor,
            current_version=current_version,
        )

    def can_apply_task_event(
        self,
        current_state: TaskState,
        event: TaskEvent,
        *,
        actor: Actor = Actor.SCHEDULER,
    ) -> bool:
        """检查转换是否合法，不抛异常。供 ``process_event`` 在写 control 前预校验。"""
        return self._state_machine.can_transition(current_state, event, actor=actor)


# --------------------------------------------------------------------------- #
# TASK-015: 初始化 control 分支
# --------------------------------------------------------------------------- #


#: ``.maf`` 协议目录在 control 分支上的固定位置。
_MAF_DIR_NAME: str = ".maf"

#: ``project.yaml`` 模板中的占位符，初始化时替换为真实 ``project_id``。
_PROJECT_ID_PLACEHOLDER: str = "replace-with-project-id"

#: 初始 control commit message。固定字符串保证幂等：重复初始化产生的 commit message
#: 一致，便于审计比对。
_INIT_COMMIT_MESSAGE: str = "maf: initialize control branch and .maf protocol"


class LocalGitCoordinationService:
    """``GitCoordinationService`` 的具体实现（TASK-015）：初始化 control 分支。

    单写协调协议要求 ``maf/control`` 是中央调度器唯一写入的分支；本类只实现
    ``initialize_project``，用于在项目仓库初始化时创建 ``maf/control`` 分支并写入
    ``.maf/`` 协议目录。其他 Git 协调方法（``publish_tasks``、``process_event``、
    ``sync``）由后续任务（TASK-016 至 TASK-021）填充。

    设计决策：

    - **GitCli 复用**：所有 git 命令经注入的 :class:`GitCli` 执行，凭据经
      ``extra_env`` 由调用方注入；本类不持有明文凭据。
    - **模板来源**：``templates/git_coordination/`` 下的 ``project.yaml``、
      ``status.md``、``PROTOCOL.md``、``schemas/``、``tasks/``、``nodes/``、
      ``events/``。可在构造时注入自定义 ``templates_dir`` 用于测试隔离。
    - **Schema 校验**：使用 :class:`SchemaLoader` 校验写入的 ``project.yaml``
      与 ``project-v1.schema.json`` 一致，确保初始化产出符合协议。
    - **幂等性**：已有 ``maf/control`` 分支则读取其 ``.maf/project.yaml``，
      校验 ``schema_version`` / ``control_branch`` / ``coordination_mode``
      兼容；兼容返回当前 control commit，不重复写入。不兼容抛
      :class:`UnsupportedOperationError`，**不覆盖**。
    - **不修改 main**：操作前后切回原分支；写入仅发生在 ``maf/control``
      分支上。``main`` 工作树不留 ``.maf/`` 痕迹。
    - **状态转换保留**：组合 :class:`GitCoordinationStateService` 以便后续
      任务（TASK-016~021）在同一个服务实例上扩展，且 TASK-022 的状态机
      能力对调用方仍然可见。
    """

    def __init__(
        self,
        *,
        git_cli: Any,
        repository_path: str,
        control_branch: str = "maf/control",
        default_branch: str = "main",
        templates_dir: Path | None = None,
        schema_loader: SchemaLoader | None = None,
        state_service: GitCoordinationStateService | None = None,
        logger: Any = None,
    ) -> None:
        if not control_branch:
            raise ArgumentError("control_branch must not be empty")
        if not default_branch:
            raise ArgumentError("default_branch must not be empty")
        self._git_cli: Any = git_cli
        self._repository_path: str = repository_path
        self._control_branch: str = control_branch
        self._default_branch: str = default_branch
        self._templates_dir: Path = (
            templates_dir if templates_dir is not None else self._default_templates_dir()
        )
        self._schema_loader: SchemaLoader = schema_loader or SchemaLoader()
        self._state_service: GitCoordinationStateService = (
            state_service or GitCoordinationStateService()
        )
        self._log: Any = logger or structlog.get_logger("maf.git_coordination")

    # ------------------------------------------------------------------ #
    # 公共属性
    # ------------------------------------------------------------------ #

    @property
    def state_service(self) -> GitCoordinationStateService:
        """暴露 :class:`GitCoordinationStateService` 以便调用方复用 TASK-022 能力。"""
        return self._state_service

    @property
    def control_branch(self) -> str:
        return self._control_branch

    @property
    def default_branch(self) -> str:
        return self._default_branch

    # ------------------------------------------------------------------ #
    # GitCoordinationService: initialize_project
    # ------------------------------------------------------------------ #

    async def initialize_project(self, repository_binding_id: str, project_id: str) -> str:
        """在 ``repository_path`` 创建或验证 ``maf/control`` 与 ``.maf`` 协议文件。

        参数：
            repository_binding_id: 触发初始化的仓库绑定 ID（仅用于日志/审计，
                不进入 control 内容）。
            project_id: 写入 ``.maf/project.yaml`` 的项目标识，必须非空且不含
                YAML 控制字符。

        返回：``maf/control`` 分支当前 HEAD commit hash（40 字符 SHA-1）。

        异常：
            ArgumentError: ``project_id`` 为空或模板缺失。
            UnsupportedOperationError: 已存在 ``maf/control`` 但协议版本/字段
                不兼容；本类**不覆盖**已有协议。
            ExternalDependencyError: git 命令执行失败（由 GitCli 抛出）。
            ValidationError: 生成的 ``project.yaml`` 不符合 ``project-v1`` Schema。

        流程：

        1. **校验入参**：``project_id`` 非空。
        2. **检查 ``maf/control`` 是否已存在**。
        3. **已存在 → 幂等校验**：读取 ``.maf/project.yaml``，校验
           ``schema_version``、``control_branch``、``coordination_mode`` 与
           协议常量一致；一致返回当前 HEAD（不写新 commit），不一致抛
           ``UnsupportedOperationError``（**不覆盖**）。
        4. **不存在 → 创建分支并写入协议目录**：
           a. 记录当前 HEAD 分支名（用于最后切回，避免污染 main 工作树）。
           b. 从 ``default_branch`` 创建 ``maf/control``（``git branch``）。
           c. 切换到 ``maf/control``（``git switch``）。
           d. 复制模板到 ``.maf/``，替换 ``project.yaml`` 中的 ``project_id`` 占位符。
           e. 通过 :class:`SchemaLoader` 校验 ``project.yaml``。
           f. ``git add .maf`` + ``git commit``。
           g. 切回原分支（即使是 detached HEAD 也切回）。
        5. 返回新 ``maf/control`` 的 HEAD commit。
        """
        if not project_id:
            raise ArgumentError("project_id must not be empty")
        if _PROJECT_ID_PLACEHOLDER == project_id:
            raise ArgumentError(
                "project_id must be a real value, not the template placeholder"
            )

        binding_label = repository_binding_id or "<no-binding>"
        self._log.info(
            "initialize_project_start",
            repository_binding_id=binding_label,
            project_id=project_id,
            control_branch=self._control_branch,
        )

        control_exists = await self._branch_exists(self._control_branch)
        if control_exists:
            commit = await self._verify_existing_control(project_id)
            self._log.info(
                "initialize_project_idempotent",
                repository_binding_id=binding_label,
                control_commit=commit,
            )
            return commit

        commit = await self._create_control_branch_and_write_protocol(project_id)
        self._log.info(
            "initialize_project_created",
            repository_binding_id=binding_label,
            control_commit=commit,
        )
        return commit

    # ------------------------------------------------------------------ #
    # 已有 control 分支：幂等校验
    # ------------------------------------------------------------------ #

    async def _verify_existing_control(self, project_id: str) -> str:
        """读取已有 ``.maf/project.yaml``，校验兼容性后返回 control HEAD。

        兼容性判定：
        - ``schema_version`` 等于 :class:`ProtocolVersion` 最新版本；
        - ``control_branch`` 等于本服务配置的 ``control_branch``；
        - ``coordination_mode`` 等于 ``git_single_writer``。

        任何不一致都视为不兼容协议，抛 ``UnsupportedOperationError``，
        **不覆盖**已有 control。
        """
        commit = await self._rev_parse(self._control_branch)

        # 通过 ``git show <branch>:.maf/project.yaml`` 读取，不切分支、不污染工作树。
        rc, out, err = await self._git_cli.run(
            self._repository_path,
            ["show", f"{self._control_branch}:{_MAF_DIR_NAME}/project.yaml"],
            0,
        )
        if rc != 0:
            raise UnsupportedOperationError(
                f"existing {self._control_branch} branch is missing "
                f".maf/project.yaml: {err.strip()}",
                context={
                    "control_branch": self._control_branch,
                    "control_commit": commit,
                    "reason": "missing_project_yaml",
                },
            )

        try:
            existing = yaml.safe_load(out) or {}
        except yaml.YAMLError as exc:
            raise UnsupportedOperationError(
                f"existing .maf/project.yaml is not valid YAML: {exc}",
                context={
                    "control_branch": self._control_branch,
                    "control_commit": commit,
                    "reason": "invalid_yaml",
                },
            ) from exc

        if not isinstance(existing, dict):
            raise UnsupportedOperationError(
                "existing .maf/project.yaml is not an object",
                context={
                    "control_branch": self._control_branch,
                    "control_commit": commit,
                    "reason": "non_object_yaml",
                },
            )

        # 校验协议关键字段（任何一个不匹配都视为不兼容，停止而非覆盖）。
        actual_version = existing.get("schema_version")
        actual_control = existing.get("control_branch")
        actual_mode = existing.get("coordination_mode")

        issues: list[str] = []
        if actual_version != ProtocolVersion.latest().value:
            issues.append(
                f"schema_version={actual_version!r} "
                f"(expected {ProtocolVersion.latest().value})"
            )
        if actual_control != self._control_branch:
            issues.append(
                f"control_branch={actual_control!r} "
                f"(expected {self._control_branch!r})"
            )
        if actual_mode != "git_single_writer":
            issues.append(
                f"coordination_mode={actual_mode!r} "
                "(expected 'git_single_writer')"
            )

        if issues:
            raise UnsupportedOperationError(
                "existing maf/control branch uses an incompatible protocol; "
                "refusing to overwrite (initialize must stop on incompatible protocol)",
                context={
                    "control_branch": self._control_branch,
                    "control_commit": commit,
                    "issues": issues,
                    "reason": "incompatible_protocol",
                },
            )

        # 兼容：返回当前 HEAD，不写新 commit（幂等）。
        return commit

    # ------------------------------------------------------------------ #
    # 新建 control 分支并写入 .maf/
    # ------------------------------------------------------------------ #

    async def _create_control_branch_and_write_protocol(self, project_id: str) -> str:
        """从 default_branch 创建 maf/control，写入 .maf/ 协议目录并提交。

        关键决策：

        - **不修改 main 工作树**：``git switch`` 之前先用 ``git rev-parse --abbrev-ref``
          记录当前分支名（detached HEAD 时记录 commit），最后切回。
        - **从 default_branch 创建**：``git branch <control> <default>`` 不切换工作树；
          随后 ``git switch <control>`` 切换并写入文件。
        - **空仓库（unborn HEAD）**：当 ``default_branch`` 不存在且 ``HEAD`` 无法
          rev-parse 时，视为 unborn HEAD；使用 ``git switch --orphan`` 创建
          无父提交的 ``maf/control``，满足"空仓库可幂等初始化"验收标准。
        - **不使用 ``--orphan`` 处理非空仓库**：control 分支共享 main 历史，
          便于审阅 control 的起源。
        - **只 add .maf**：避免误把工作树中其他未跟踪文件提交进 control。
        - **失败时回滚**：写入或 commit 失败时清理 ``.maf/`` 目录并切回原分支，
          避免污染 main 工作树。
        """
        repo_path = self._repository_path
        default_exists = await self._branch_exists(self._default_branch)

        # 检测 unborn HEAD（空仓库）：``git rev-parse --verify HEAD`` 失败表示
        # 仓库还没有任何提交。
        head_unborn = False
        if not default_exists:
            rc, _out, _err = await self._git_cli.run(
                repo_path,
                ["rev-parse", "--verify", "HEAD"],
                0,
            )
            head_unborn = rc != 0
            if head_unborn:
                self._log.info(
                    "initialize_project_unborn_head",
                    default_branch=self._default_branch,
                    note="using --orphan to create maf/control on empty repo",
                )

        # 1. 记录当前分支/commit，便于最后切回（不污染 main 工作树）。
        #    unborn HEAD 时无需切回（main 不存在），返回空标记。
        original_ref = (
            "" if head_unborn else await self._current_ref_for_restore()
        )

        # 2. 创建 maf/control 并切换。
        if head_unborn:
            # 空仓库：用 --orphan 创建无父提交的 control 分支。
            rc, _out, err = await self._git_cli.run(
                repo_path,
                ["switch", "--orphan", self._control_branch],
                0,
            )
        else:
            # 从 default_branch 创建分支，再切换。
            rc, _out, err = await self._git_cli.run(
                repo_path,
                ["branch", self._control_branch, self._default_branch],
                0,
            )
            if rc == 0:
                rc, _out, err = await self._git_cli.run(
                    repo_path,
                    ["switch", self._control_branch],
                    0,
                )
        if rc != 0:
            await self._cleanup_maf_and_restore(original_ref)
            raise ExternalDependencyError(
                f"failed to create/switch to {self._control_branch!r}: "
                f"{err.strip()}",
                context={"stderr": err, "returncode": rc},
                retryable=False,
            )

        # 3. 写入 .maf/ 协议目录（在 maf/control 工作树上）。
        try:
            await self._write_maf_protocol(project_id)
        except Exception:
            # 写入失败：清理 .maf 目录，切回原分支。
            await self._cleanup_maf_and_restore(original_ref)
            raise

        # 4. git add .maf && git commit。
        rc, _out, err = await self._git_cli.run(
            repo_path,
            ["add", ".maf"],
            0,
        )
        if rc != 0:
            await self._cleanup_maf_and_restore(original_ref)
            raise ExternalDependencyError(
                f"git add .maf failed: {err.strip()}",
                context={"stderr": err, "returncode": rc},
                retryable=False,
            )

        rc, _out, err = await self._git_cli.run(
            repo_path,
            ["commit", "-m", _INIT_COMMIT_MESSAGE, "--no-edit"],
            0,
        )
        if rc != 0:
            await self._cleanup_maf_and_restore(original_ref)
            raise ExternalDependencyError(
                f"git commit on {self._control_branch!r} failed: {err.strip()}",
                context={"stderr": err, "returncode": rc},
                retryable=False,
            )

        # 5. 切回原分支（不污染 main 工作树）。
        #    unborn HEAD 时不切回（main 不存在，保留在 maf/control 上是安全状态）。
        if original_ref:
            await self._restore_ref(original_ref)
        else:
            # 空仓库初始化后工作树仍停在 maf/control；调用方可后续 git switch main。
            self._log.info(
                "initialize_project_skip_restore_on_unborn",
                note="empty repo; working tree stays on maf/control",
            )

        # 6. 返回 maf/control 的 HEAD commit。
        return await self._rev_parse(self._control_branch)

    # ------------------------------------------------------------------ #
    # TASK-016: fetch_control —— 只读访问 control 快照
    # ------------------------------------------------------------------ #

    async def fetch_control(self, project_id: str) -> CoordinationSnapshot:
        """只读访问 ``maf/control`` 分支当前快照，返回 :class:`CoordinationSnapshot`。

        实现要点：

        - **只读 git 命令**：``git rev-parse`` / ``git log`` / ``git show`` / ``git ls-tree``，
          全部不修改工作区、不切换分支、不 push；
        - **原子性**：任一关键文件缺失或 Schema 错误时抛异常，不返回部分快照；
        - **去重**：snapshot 携带 ``control_commit``，调用方可据此跳过相同 commit 的重复处理；
        - **project_id 校验**：传入的 ``project_id`` 必须与 control 上 ``.maf/project.yaml``
          中的 ``project_id`` 一致，防止跨项目误读。

        参数：
            project_id: 期望的项目 ID；为空抛 :class:`ArgumentError`。

        异常：
            ArgumentError: ``project_id`` 为空，或与 control 上 ``.maf/project.yaml`` 不一致。
            UnsupportedOperationError: ``maf/control`` 分支不存在或缺少 ``.maf/project.yaml``。
            ExternalDependencyError: git 只读命令执行失败。
            ValidationError: ``.maf/project.yaml`` 不符合 ``project-v1`` Schema。
        """
        if not project_id:
            raise ArgumentError("project_id must not be empty")

        self._log.info(
            "fetch_control_start",
            project_id=project_id,
            control_branch=self._control_branch,
        )

        # 1. control 分支必须存在。
        if not await self._branch_exists(self._control_branch):
            raise UnsupportedOperationError(
                f"control branch {self._control_branch!r} does not exist; "
                "call initialize_project first",
                context={
                    "control_branch": self._control_branch,
                    "reason": "control_branch_missing",
                },
            )

        # 2. 读取 control HEAD commit。
        control_commit = await self._rev_parse(self._control_branch)

        # 3. 读取 commit 时间戳（ISO 8601，UTC）。
        commit_timestamp = await self._get_commit_timestamp(self._control_branch)

        # 4. 读取并解析 .maf/project.yaml。
        project_yaml_text = await self._read_file_from_branch(
            self._control_branch, f"{_MAF_DIR_NAME}/project.yaml"
        )
        try:
            project_yaml = yaml.safe_load(project_yaml_text) or {}
        except yaml.YAMLError as exc:
            raise UnsupportedOperationError(
                f".maf/project.yaml is not valid YAML: {exc}",
                context={
                    "control_branch": self._control_branch,
                    "control_commit": control_commit,
                    "reason": "invalid_yaml",
                },
            ) from exc

        if not isinstance(project_yaml, dict):
            raise UnsupportedOperationError(
                ".maf/project.yaml is not an object",
                context={
                    "control_branch": self._control_branch,
                    "control_commit": control_commit,
                    "reason": "non_object_yaml",
                },
            )

        # 5. Schema 校验 project.yaml。
        self._schema_loader.validate(
            SchemaRef(name="project", version=ProtocolVersion.latest().value),
            project_yaml,
            source_file=Path(f"{self._control_branch}:{_MAF_DIR_NAME}/project.yaml"),
        )

        # 6. 校验 project_id 一致性（防止跨项目误读）。
        actual_project_id = project_yaml.get("project_id")
        if actual_project_id != project_id:
            raise ArgumentError(
                f"project_id mismatch: expected {project_id!r}, "
                f"control has {actual_project_id!r}",
                context={
                    "expected_project_id": project_id,
                    "actual_project_id": actual_project_id,
                    "control_commit": control_commit,
                },
            )

        # 7. 读取 .maf/status.md（缺失时视为协议错误）。
        status_md = await self._read_file_from_branch(
            self._control_branch, f"{_MAF_DIR_NAME}/status.md"
        )

        # 8. 列出 tasks/nodes/events 目录。
        tasks_paths = await self._list_dir_from_branch(
            self._control_branch, f"{_MAF_DIR_NAME}/tasks/"
        )
        nodes_paths = await self._list_dir_from_branch(
            self._control_branch, f"{_MAF_DIR_NAME}/nodes/"
        )
        events_paths = await self._list_dir_from_branch(
            self._control_branch, f"{_MAF_DIR_NAME}/events/"
        )

        # 9. 构造快照（tasks/nodes 解析留给 TASK-017/019 等后续任务，此处为空列表占位）。
        snapshot: CoordinationSnapshot = {
            "project_id": project_id,
            "control_commit": control_commit,
            "commit_timestamp": commit_timestamp,
            "project_yaml": project_yaml,
            "status_md": status_md,
            "tasks_paths": tasks_paths,
            "nodes_paths": nodes_paths,
            "events_paths": events_paths,
            "tasks": [],
            "nodes": [],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        self._log.info(
            "fetch_control_ok",
            project_id=project_id,
            control_commit=control_commit,
            commit_timestamp=commit_timestamp,
            tasks_count=len(tasks_paths),
            nodes_count=len(nodes_paths),
            events_count=len(events_paths),
        )
        return snapshot

    # ------------------------------------------------------------------ #
    # TASK-019: discover_node_events / discover_all_node_events
    # ------------------------------------------------------------------ #

    #: Event file directory prefix on node branches (aligns with
    #: ``maf_contracts.coordination.build_event_file_path``).
    _EVENT_DIR_PREFIX: str = ".maf/events/"

    async def discover_node_events(
        self,
        project_id: str,
        node_id: str,
        *,
        since_commit: str | None = None,
    ) -> DiscoveredEvents:
        """Discover new events on ``maf/node/<node-id>`` (read-only).

        See :class:`EventDiscoveryService` for the design guarantees.

        Parameters:
            project_id: project context (validated against control; when the
                control branch is missing the call still succeeds but logs a
                warning, because node branches can exist before control is
                fetched).
            node_id: the node whose branch to scan.
            since_commit: last processed HEAD of the node branch; ``None`` for
                full scan.

        Returns:
            :class:`DiscoveredEvents` with valid events sorted by ``event_id``
            and invalid files reported in ``invalid_events``.

        Raises:
            ArgumentError: ``project_id`` or ``node_id`` is empty.
        """
        if not project_id:
            raise ArgumentError("project_id must not be empty")
        if not node_id:
            raise ArgumentError("node_id must not be empty")

        branch = build_node_branch_name(node_id)
        self._log.info(
            "discover_node_events_start",
            project_id=project_id,
            node_id=node_id,
            branch=branch,
            since_commit=since_commit,
        )

        # 1. Branch must exist locally (caller is responsible for fetching
        #    node branches before discovery).
        if not await self._branch_exists(branch):
            self._log.info(
                "discover_node_events_branch_missing",
                project_id=project_id,
                node_id=node_id,
                branch=branch,
            )
            return {
                "node_id": node_id,
                "branch": branch,
                "branch_exists": False,
                "latest_commit": None,
                "events": [],
                "invalid_events": [],
                "diverged": False,
                "scanned_paths": [],
            }

        # 2. Current HEAD of the node branch.
        head = await self._rev_parse(branch)

        # 3. Fast path: since_commit == head → no new events.
        if since_commit is not None and since_commit == head:
            self._log.info(
                "discover_node_events_no_new_events",
                project_id=project_id,
                node_id=node_id,
                branch=branch,
                latest_commit=head,
            )
            return {
                "node_id": node_id,
                "branch": branch,
                "branch_exists": True,
                "latest_commit": head,
                "events": [],
                "invalid_events": [],
                "diverged": False,
                "scanned_paths": [],
            }

        # 4. Determine which event files to read.
        diverged = False
        if since_commit is None:
            event_paths = await self._list_dir_from_branch(
                branch, self._EVENT_DIR_PREFIX
            )
        else:
            diverged = await self._is_diverged(since_commit, head)
            if diverged:
                # Force-push / history rollback: fall back to full scan so no
                # events are missed. Caller should isolate the branch.
                self._log.warning(
                    "discover_node_events_diverged",
                    project_id=project_id,
                    node_id=node_id,
                    branch=branch,
                    since_commit=since_commit,
                    latest_commit=head,
                )
                event_paths = await self._list_dir_from_branch(
                    branch, self._EVENT_DIR_PREFIX
                )
            else:
                event_paths = await self._list_changed_event_files(
                    since_commit, head
                )

        # 5. Filter to .json files and read/validate each.
        events: list[CoordinationEvent] = []
        invalid_events: list[InvalidEventEntry] = []
        scanned: list[str] = []
        for path in event_paths:
            if not path.endswith(".json"):
                continue
            scanned.append(path)
            try:
                content = await self._read_file_from_branch(branch, path)
            except UnsupportedOperationError as exc:
                invalid_events.append(
                    InvalidEventEntry(
                        path=path,
                        error=f"read_failed: {exc.message}",
                        raw_content=None,
                    )
                )
                continue
            try:
                raw = json.loads(content)
            except (json.JSONDecodeError, ValueError) as exc:
                invalid_events.append(
                    InvalidEventEntry(
                        path=path,
                        error=f"invalid_json: {exc}",
                        raw_content=content,
                    )
                )
                continue
            try:
                model = CoordinationEventModel.model_validate(raw)
            except Exception as exc:  # noqa: BLE001 - report all validation errors
                invalid_events.append(
                    InvalidEventEntry(
                        path=path,
                        error=f"schema_validation_failed: {exc}",
                        raw_content=content,
                    )
                )
                continue
            events.append(model.model_dump(mode="json"))

        # 6. Deterministic sort: event_id (not machine time) for stable replay.
        events.sort(key=lambda e: e["event_id"])
        invalid_events.sort(key=lambda i: i["path"])

        self._log.info(
            "discover_node_events_ok",
            project_id=project_id,
            node_id=node_id,
            branch=branch,
            latest_commit=head,
            events_count=len(events),
            invalid_count=len(invalid_events),
            diverged=diverged,
        )
        return {
            "node_id": node_id,
            "branch": branch,
            "branch_exists": True,
            "latest_commit": head,
            "events": events,
            "invalid_events": invalid_events,
            "diverged": diverged,
            "scanned_paths": scanned,
        }

    async def discover_all_node_events(
        self,
        project_id: str,
        *,
        since_commit: str | None = None,
    ) -> dict[str, DiscoveredEvents]:
        """Discover new events across all ``maf/node/*`` branches (read-only).

        Enumerates local node branches via ``git for-each-ref`` and delegates to
        :meth:`discover_node_events` for each. ``since_commit`` (when provided)
        is applied to every node branch; callers needing per-node watermarks
        should call :meth:`discover_node_events` individually.

        Returns a dict keyed by ``node_id``. Branches whose names do not follow
        the ``maf/node/<node-id>`` convention are skipped.
        """
        if not project_id:
            raise ArgumentError("project_id must not be empty")

        branches = await self._list_node_branches()
        self._log.info(
            "discover_all_node_events_start",
            project_id=project_id,
            node_branches=branches,
            since_commit=since_commit,
        )

        results: dict[str, DiscoveredEvents] = {}
        for branch in branches:
            # Extract node_id from ``maf/node/<node-id>``.
            if not branch.startswith(NODE_BRANCH_PREFIX):
                continue
            node_id = branch[len(NODE_BRANCH_PREFIX):]
            if not node_id:
                continue
            results[node_id] = await self.discover_node_events(
                project_id,
                node_id,
                since_commit=since_commit,
            )

        self._log.info(
            "discover_all_node_events_ok",
            project_id=project_id,
            nodes_count=len(results),
            since_commit=since_commit,
        )
        return results

    # ------------------------------------------------------------------ #
    # TASK-020: verify_node_identity —— 节点 Git 身份验证
    # ------------------------------------------------------------------ #

    #: 验证方法标识。MVP 使用 commit author email 比对；完整 GPG/SSH 签名
    #: 验证由后续任务增强（TASK-020 明确不包含密钥签发）。
    _VERIFICATION_METHOD_COMMIT_AUTHOR: str = "commit_author_email"

    async def verify_node_identity(
        self,
        event: CoordinationEventModel,
        *,
        expected_node_id: str,
    ) -> NodeIdentity:
        """验证节点 Git 身份（TASK-020）。

        验证流程：

        1. **source 一致性**：``event.node_id`` 必须等于 ``expected_node_id``，
           否则视为冒用其他节点身份（``EVENT_NODE_IDENTITY_MISMATCH``）；
        2. **节点已注册**：从 ``maf/control:.maf/nodes/<node-id>.yaml`` 读取已
           注册节点清单。非 ``NODE_REGISTERED`` 事件要求节点必须已注册，
           否则拒绝（``EVENT_NODE_UNKNOWN``，**不自动注册**）；
        3. ``NODE_REGISTERED`` 事件允许使用 ``payload.manifest`` 作为声明身份
           （trust-on-first-use；中央调度器验证通过后才写入 ``control/nodes/``）；
        4. **manifest node_id 一致**：清单中的 ``node_id`` 必须与
           ``expected_node_id`` 一致；
        5. **commit author 验证**（MVP 签名验证降级）：读取事件文件在节点分支
           上的 commit author email，与清单 ``git_identity.email`` 比对；
        6. 通过后返回 :class:`NodeIdentity`（``verified=True``）。

        参数：
            event: 待验证的协调事件（已通过 :class:`CoordinationEventModel` 校验）。
            expected_node_id: 期望的节点 ID（通常来自节点分支名 ``maf/node/<id>``）。

        返回：:class:`NodeIdentity`，含 node_id、verified、verification_method、
            commit_author、manifest 等字段。

        异常：
            ArgumentError: ``expected_node_id`` 为空。
            NodeIdentityError: 任一验证步骤失败，``reason_code`` 为
                ``EVENT_NODE_IDENTITY_MISMATCH`` 或 ``EVENT_NODE_UNKNOWN``，
                ``context`` 含 ``node_id``、``event_id``、``failure_reason``。
        """
        if not expected_node_id:
            raise ArgumentError("expected_node_id must not be empty")

        # 1. source 一致性校验。
        if event.node_id != expected_node_id:
            raise NodeIdentityError(
                f"event source node_id {event.node_id!r} does not match "
                f"expected {expected_node_id!r}",
                reason_code=ReasonCode.EVENT_NODE_IDENTITY_MISMATCH,
                context={
                    "node_id": expected_node_id,
                    "event_node_id": event.node_id,
                    "event_id": event.event_id,
                    "failure_reason": "source_mismatch",
                },
            )

        # 2. 读取已注册节点清单（只读访问 control 分支）。
        manifest = await self._read_registered_manifest(expected_node_id)

        # 3. 未知节点拒绝（不自动注册）。
        if manifest is None:
            if event.event_type == "NODE_REGISTERED":
                # NODE_REGISTERED 事件允许使用 payload.manifest 作为声明身份
                #（trust-on-first-use；中央调度器验证通过后才持久化到 control/nodes/）。
                payload_manifest = (event.payload or {}).get("manifest")
                if not isinstance(payload_manifest, dict):
                    raise NodeIdentityError(
                        f"NODE_REGISTERED event {event.event_id!r} from unregistered "
                        f"node {expected_node_id!r} has no manifest in payload",
                        reason_code=ReasonCode.EVENT_NODE_UNKNOWN,
                        context={
                            "node_id": expected_node_id,
                            "event_id": event.event_id,
                            "failure_reason": "missing_payload_manifest",
                        },
                    )
                manifest = cast(NodeManifest, payload_manifest)
            else:
                # 非 NODE_REGISTERED 事件来自未注册节点：拒绝，不自动注册。
                raise NodeIdentityError(
                    f"node {expected_node_id!r} is not registered; event "
                    f"{event.event_id!r} rejected (no auto-registration)",
                    reason_code=ReasonCode.EVENT_NODE_UNKNOWN,
                    context={
                        "node_id": expected_node_id,
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "failure_reason": "node_not_registered",
                    },
                )

        # 4. manifest node_id 一致性校验（防止 control/nodes/ 文件被错放）。
        manifest_node_id = manifest.get("node_id")
        if manifest_node_id != expected_node_id:
            raise NodeIdentityError(
                f"manifest node_id {manifest_node_id!r} does not match expected "
                f"{expected_node_id!r}",
                reason_code=ReasonCode.EVENT_NODE_IDENTITY_MISMATCH,
                context={
                    "node_id": expected_node_id,
                    "manifest_node_id": manifest_node_id,
                    "event_id": event.event_id,
                    "failure_reason": "manifest_node_id_mismatch",
                },
            )

        # 5. 读取事件 commit author（来自节点分支上的事件文件 commit）。
        commit_author = await self._get_event_commit_author(
            expected_node_id, event.event_id
        )

        # 6. commit author email 与清单声明身份比对（MVP 签名验证降级）。
        declared_identity = extract_node_identity_from_manifest(manifest)
        if not verify_commit_author(commit_author, declared_identity):
            raise NodeIdentityError(
                f"commit author email {commit_author.get('email')!r} does not match "
                f"declared email {declared_identity.get('email')!r} for node "
                f"{expected_node_id!r}",
                reason_code=ReasonCode.EVENT_NODE_IDENTITY_MISMATCH,
                context={
                    "node_id": expected_node_id,
                    "event_id": event.event_id,
                    "commit_author_email": commit_author.get("email", ""),
                    "declared_email": declared_identity.get("email", ""),
                    "failure_reason": "commit_author_mismatch",
                },
            )

        self._log.info(
            "verify_node_identity_ok",
            node_id=expected_node_id,
            event_id=event.event_id,
            event_type=event.event_type,
            verification_method=self._VERIFICATION_METHOD_COMMIT_AUTHOR,
        )

        return NodeIdentity(
            node_id=expected_node_id,
            verified=True,
            verification_method=self._VERIFICATION_METHOD_COMMIT_AUTHOR,
            commit_author=commit_author,
            manifest=manifest,
            failure_reason="",
        )

    # ------------------------------------------------------------------ #
    # TASK-025: process_event(PROGRESS/BLOCKED)
    # ------------------------------------------------------------------ #

    #: 默认 consumer_id（与 ``EventDecisionRepository`` 主键 ``consumer_id`` 列对齐）。
    #: 调用方可在 ``process_event`` 入参中覆盖，便于多个消费者独立记录判定。
    _DEFAULT_PROCESS_EVENT_CONSUMER_ID: str = "process_event"

    #: ``PROGRESS_REPORTED`` 事件 payload 必填字段（与协议 §8 进度字段对齐）。
    _PROGRESS_REQUIRED_FIELDS: tuple[str, ...] = ("progress_percent",)

    #: ``BLOCKED_REPORTED`` 事件 payload 必填字段（与协议 §8 阻塞字段对齐）。
    _BLOCKED_REQUIRED_FIELDS: tuple[str, ...] = ("block_reason",)

    #: ``PROGRESS_REPORTED`` 事件 payload ``progress_percent`` 合法区间（闭区间）。
    _PROGRESS_PERCENT_MIN: int = 0
    _PROGRESS_PERCENT_MAX: int = 100

    async def process_event(
        self,
        event: CoordinationEventModel,
        *,
        current_epoch: int,
        repository: EventConsumer,
        current_state: TaskState = TaskState.IN_PROGRESS,
        consumer_id: str = _DEFAULT_PROCESS_EVENT_CONSUMER_ID,
    ) -> ProcessResult:
        """处理 ``PROGRESS_REPORTED`` / ``BLOCKED_REPORTED`` 事件（TASK-025）。

        处理流程（严格按顺序）：

        1. **幂等校验**（TASK-021）：调用 :func:`has_processed_event` 检查
           ``event_id`` 是否已被 ``consumer_id`` 成功处理。已处理 → 记录
           ``skipped_duplicate`` 判定并返回 :class:`ProcessResult`
           （``processed=False``，``new_state=None``），不重复应用副作用；
        2. **epoch fencing 校验**（TASK-024）：调用 :func:`check_assignment_epoch`
           比较 ``event.assignment_epoch`` 与 ``current_epoch``。失败 → 记录
           ``skipped_stale_epoch`` / ``skipped_future_epoch`` 判定并返回
           :class:`ProcessResult`（``processed=False``，``new_state=None``），
           旧任务分支不删除（作为可选恢复材料）；
        3. **事件分发**：根据 ``event.event_type`` 分发到 :meth:`_handle_progress`
           或 :meth:`_handle_blocked`；其他事件类型（如 ``SUBMISSION_CREATED``）
           抛 :class:`UnsupportedOperationError`（TASK-025 只处理 PROGRESS/BLOCKED，
           SUBMISSION/DONE 由 TASK-026 处理）；
        4. **记录成功判定**：成功处理后调用 :func:`record_event_decision` 记录
           ``applied`` 判定，``result`` 摘要含 ``task_id`` 与 ``new_state``；
        5. **错误处理**：payload 缺失/非法或状态转换不合法时，记录 ``failed``
           判定后重新抛出异常（与 :func:`process_event_idempotently` 一致），
           调用方可在事务内捕获并提交以持久化 ``failed`` 跟踪记录。

        参数：
            event: 已通过 :class:`CoordinationEventModel` 校验的协调事件。
                ``event_type`` 必须是 ``PROGRESS_REPORTED`` 或 ``BLOCKED_REPORTED``。
            current_epoch: 当前权威 epoch（来自 SQLite 投影，由 TASK-027 维护；
                必须 >= 1）。
            repository: 绑定到当前 ``UnitOfWork`` 事务连接的判定仓库
                （:class:`EventConsumer`），用于幂等查询与判定记录。
            current_state: 任务当前状态。BLOCKED 事件通过
                :class:`TaskStateMachine.transition` 校验 ``current_state → BLOCKED``
                转换合法性；PROGRESS 事件不改变状态，此参数仅用于审计。
                默认 :data:`TaskState.IN_PROGRESS`（与协议 §8 阻塞分支约定一致）。
            consumer_id: 消费者标识，默认 ``"process_event"``；多消费者场景下
                各自独立记录判定。

        返回：:class:`ProcessResult`。成功 → ``processed=True``，``new_state``
            为新状态（PROGRESS 为 ``None``，BLOCKED 为 ``"BLOCKED"``）；
            跳过 → ``processed=False``，``new_state=None``。

        异常：
            ArgumentError: ``consumer_id`` 为空。
            UnsupportedOperationError: ``event.event_type`` 不是
                ``PROGRESS_REPORTED`` 或 ``BLOCKED_REPORTED``（TASK-025 范围外）。
            ValidationError: PROGRESS payload 缺少 ``progress_percent`` 或
                值超出 0-100；BLOCKED payload 缺少 ``block_reason``。
            UnsupportedOperationError: BLOCKED 事件在 ``current_state`` 下
                状态转换不合法（由 :class:`TaskStateMachine.transition` 抛出）。

        设计决策：

        - **不修改事件内容**：事件是 Git coordination 事实源，本方法只读
          ``event.payload``，不修改事件 dict（与 TASK-021 验收一致）。
        - **不更新 SQLite 投影**：本任务范围不含 SQLite 投影（由 TASK-027
          维护）；``process_event`` 只提供处理接口与判定记录，调用方据此
          后续推进投影。
        - **PROGRESS 不改状态**：PROGRESS 事件只更新进度字段
          （``progress_percent`` / ``current_step`` / ``message``），
          不调用 :class:`TaskStateMachine.transition`，任务状态保持 ``IN_PROGRESS``。
        - **BLOCKED 走状态机**：BLOCKED 事件通过
          :meth:`GitCoordinationStateService.apply_task_event` 完成
          ``IN_PROGRESS → BLOCKED`` 转换（也接受 ``ASSIGNED`` /
          ``REWORK_REQUIRED`` 作为源状态，由状态机校验）。
        - **epoch fencing 拒绝不抛异常**：旧/未来 epoch 是预期内的 fencing
          拒绝，返回 :class:`ProcessResult` 而非抛异常，便于调用方在
          ``sync`` 循环中继续处理后续事件；非法 payload / 状态转换是真正的
          错误，记录 ``failed`` 后抛异常。
        """
        if not consumer_id:
            raise ArgumentError("consumer_id must not be empty")
        if current_epoch < 1:
            raise ArgumentError(
                "current_epoch must be >= 1",
                context={"current_epoch": current_epoch},
            )

        event_id = event.event_id
        content_hash = compute_event_content_hash(
            event.model_dump(mode="json")
        )

        # 1. 幂等校验：同 (event_id, consumer_id) 已成功处理 → 跳过。
        if await has_processed_event(
            event_id,
            consumer_id=consumer_id,
            repository=repository,
        ):
            # 重复事件不调用 handler，记录 skipped_duplicate（与
            # process_event_idempotently 一致）；保留首次决定，has_processed
            # 返回 True 时 record_decision 是幂等无操作。
            await record_event_decision(
                event_id,
                consumer_id=consumer_id,
                decision=PROCESS_DECISION_SKIPPED_DUPLICATE,
                result=f"task_id={event.task_id}",
                error=None,
                content_hash=content_hash,
                repository=repository,
            )
            self._log.info(
                "process_event_skipped_duplicate",
                event_id=event_id,
                event_type=event.event_type,
                task_id=event.task_id,
                consumer_id=consumer_id,
            )
            return ProcessResult(
                event_id=event_id,
                processed=False,
                new_state=None,
                decision=PROCESS_DECISION_SKIPPED_DUPLICATE,
                error=None,
                reason_code=ReasonCode.EVENT_DUPLICATE.value,
            )

        # 2. epoch fencing 校验：旧/未来 epoch 拒绝，不抛异常。
        task_id = event.task_id or ""
        epoch_result = check_assignment_epoch(
            task_id,
            event.assignment_epoch,
            current_epoch=current_epoch,
        )
        if not epoch_result.passed:
            await record_event_decision(
                event_id,
                consumer_id=consumer_id,
                decision=epoch_result.decision or PROCESS_DECISION_SKIPPED_STALE,
                result=f"task_id={task_id}",
                error=epoch_result.message,
                content_hash=content_hash,
                repository=repository,
            )
            self._log.info(
                "process_event_skipped_epoch",
                event_id=event_id,
                event_type=event.event_type,
                task_id=task_id,
                outcome=epoch_result.outcome,
                event_epoch=epoch_result.event_epoch,
                current_epoch=current_epoch,
            )
            return ProcessResult(
                event_id=event_id,
                processed=False,
                new_state=None,
                decision=epoch_result.decision or PROCESS_DECISION_SKIPPED_STALE,
                error=epoch_result.message,
                reason_code=epoch_result.reason_code,
            )

        # 3. 事件分发：PROGRESS/BLOCKED 分别处理，其他类型拒绝（TASK-025 范围外）。
        try:
            if event.event_type == "PROGRESS_REPORTED":
                new_state = self._handle_progress(event)
            elif event.event_type == "BLOCKED_REPORTED":
                new_state = self._handle_blocked(event, current_state=current_state)
            else:
                raise UnsupportedOperationError(
                    f"process_event(PROGRESS/BLOCKED) does not handle "
                    f"event_type {event.event_type!r} in TASK-025 "
                    f"(SUBMISSION/DONE 由 TASK-026 处理)",
                    context={
                        "event_type": event.event_type,
                        "event_id": event_id,
                        "task_id": task_id,
                    },
                )
        except Exception as exc:
            # 5. 错误处理：记录 failed 判定后重新抛出（与
            # process_event_idempotently 一致）。调用方可在事务内捕获并
            # 提交以持久化 failed 跟踪记录。
            await record_event_decision(
                event_id,
                consumer_id=consumer_id,
                decision=PROCESS_DECISION_FAILED,
                result=None,
                error=str(exc),
                content_hash=content_hash,
                repository=repository,
            )
            self._log.warning(
                "process_event_failed",
                event_id=event_id,
                event_type=event.event_type,
                task_id=task_id,
                error=str(exc),
            )
            raise

        # 4. 成功：记录 applied 判定，result 摘要含 task_id 与 new_state。
        new_state_value = new_state.value if new_state is not None else None
        result_summary = (
            f"task_id={task_id},event_type={event.event_type},"
            f"new_state={new_state_value}"
        )
        await record_event_decision(
            event_id,
            consumer_id=consumer_id,
            decision=PROCESS_DECISION_APPLIED,
            result=result_summary,
            error=None,
            content_hash=content_hash,
            repository=repository,
        )
        self._log.info(
            "process_event_applied",
            event_id=event_id,
            event_type=event.event_type,
            task_id=task_id,
            new_state=new_state_value,
        )
        return ProcessResult(
            event_id=event_id,
            processed=True,
            new_state=new_state_value,
            decision=PROCESS_DECISION_APPLIED,
            error=None,
            reason_code=None,
        )

    def _handle_progress(
        self, event: CoordinationEventModel
    ) -> TaskState | None:
        """处理 ``PROGRESS_REPORTED`` 事件（TASK-025）。

        - **校验 payload**：``progress_percent`` 必填且在 0-100 闭区间；
          ``current_step`` / ``message`` 可选，存在时必须是字符串。
        - **不改变任务状态**：PROGRESS 事件只更新进度字段
          （``progress_percent`` / ``current_step`` / ``message``），任务状态
          保持 ``IN_PROGRESS``（协议 §8）。
        - **不更新 SQLite 投影**：本任务范围不含 SQLite 投影（由 TASK-027
          维护）；本方法只校验 payload 并返回 ``None``（表示无状态变更），
          调用方据此后续推进投影。

        参数：
            event: ``PROGRESS_REPORTED`` 事件。

        返回：``None``（PROGRESS 不改变任务状态）。

        异常：
            ValidationError: ``payload`` 缺少 ``progress_percent``、值不是
                整数、或超出 0-100 闭区间；``current_step`` / ``message``
                存在但不是字符串。
        """
        payload = event.payload or {}
        # progress_percent 必填且为整数（与 TaskProgress.percent 对齐）。
        if "progress_percent" not in payload:
            raise ValidationError(
                "PROGRESS_REPORTED payload missing required field "
                "'progress_percent'",
                context={
                    "event_id": event.event_id,
                    "task_id": event.task_id,
                    "missing_field": "progress_percent",
                },
            )
        progress_percent = payload.get("progress_percent")
        if not isinstance(progress_percent, int) or isinstance(progress_percent, bool):
            raise ValidationError(
                f"PROGRESS_REPORTED payload 'progress_percent' must be int, "
                f"got {type(progress_percent).__name__}",
                context={
                    "event_id": event.event_id,
                    "task_id": event.task_id,
                    "progress_percent": progress_percent,
                },
            )
        if not (
            self._PROGRESS_PERCENT_MIN
            <= progress_percent
            <= self._PROGRESS_PERCENT_MAX
        ):
            raise ValidationError(
                f"PROGRESS_REPORTED payload 'progress_percent' must be in "
                f"[{self._PROGRESS_PERCENT_MIN}, {self._PROGRESS_PERCENT_MAX}], "
                f"got {progress_percent}",
                context={
                    "event_id": event.event_id,
                    "task_id": event.task_id,
                    "progress_percent": progress_percent,
                },
            )

        # current_step / message 可选，存在时必须是字符串。
        for optional_field in ("current_step", "message"):
            if optional_field in payload:
                value = payload[optional_field]
                if value is not None and not isinstance(value, str):
                    raise ValidationError(
                        f"PROGRESS_REPORTED payload {optional_field!r} must be "
                        f"str or null, got {type(value).__name__}",
                        context={
                            "event_id": event.event_id,
                            "task_id": event.task_id,
                            "field": optional_field,
                        },
                    )

        # PROGRESS 不改变任务状态（仍 IN_PROGRESS），返回 None 表示无状态变更。
        return None

    def _handle_blocked(
        self,
        event: CoordinationEventModel,
        *,
        current_state: TaskState,
    ) -> TaskState:
        """处理 ``BLOCKED_REPORTED`` 事件（TASK-025）。

        - **校验 payload**：``block_reason`` 必填且为非空字符串；
          ``estimated_delay`` / ``blocked_on`` 可选。
        - **状态转换**：通过 :meth:`GitCoordinationStateService.apply_task_event`
          调用 :class:`TaskStateMachine.transition` 完成
          ``current_state → BLOCKED`` 转换。合法源状态包括 ``ASSIGNED`` /
          ``IN_PROGRESS`` / ``REWORK_REQUIRED``（与状态机转换表一致）。
        - **记录阻塞原因**：阻塞原因由 ``event.payload['block_reason']`` 携带，
          本方法只校验存在性；具体存储由 SQLite 投影（TASK-027）维护。
        - **不更新 SQLite 投影**：本任务范围不含 SQLite 投影。

        参数：
            event: ``BLOCKED_REPORTED`` 事件。
            current_state: 任务当前状态。

        返回：``TaskState.BLOCKED``（新状态）。

        异常：
            ValidationError: ``payload`` 缺少 ``block_reason`` 或值为空。
            UnsupportedOperationError: ``current_state`` 不允许
                ``BLOCKED_REPORTED`` 转换（如 ``DONE`` / ``FAILED`` / ``CANCELLED``
                终态，或 ``SUBMITTED`` / ``REVIEWING`` 等无此转换的状态），
                由 :class:`TaskStateMachine.transition` 抛出。
        """
        payload = event.payload or {}
        # block_reason 必填且为非空字符串。
        if "block_reason" not in payload:
            raise ValidationError(
                "BLOCKED_REPORTED payload missing required field "
                "'block_reason'",
                context={
                    "event_id": event.event_id,
                    "task_id": event.task_id,
                    "missing_field": "block_reason",
                },
            )
        block_reason = payload.get("block_reason")
        if not isinstance(block_reason, str) or not block_reason.strip():
            raise ValidationError(
                "BLOCKED_REPORTED payload 'block_reason' must be a non-empty "
                "string",
                context={
                    "event_id": event.event_id,
                    "task_id": event.task_id,
                    "block_reason": block_reason,
                },
            )

        # 状态转换：current_state → BLOCKED（经状态机校验合法性）。
        # 使用 Actor.SCHEDULER：中央调度器是唯一权威写者，节点事件经调度器
        # 消费后由调度器代为写入权威状态（协议 §5）。
        transition = self._state_service.apply_task_event(
            current_state,
            TaskEvent.BLOCKED_REPORTED,
            actor=Actor.SCHEDULER,
        )
        return transition.new_state

    async def _read_registered_manifest(
        self, node_id: str
    ) -> NodeManifest | None:
        """从 ``maf/control:.maf/nodes/<node-id>.yaml`` 读取已注册节点清单。

        只读：使用 ``git show`` 读取 control 分支上的 YAML 文件，不修改任何分支。
        文件不存在或解析失败时返回 ``None``（视为未注册）。

        调用方应基于 ``None`` 返回值决定是否拒绝事件（不自动注册）。
        """
        path = f"{_MAF_DIR_NAME}/nodes/{node_id}.yaml"
        rc, out, _err = await self._git_cli.run(
            self._repository_path,
            ["show", f"{self._control_branch}:{path}"],
            0,
        )
        if rc != 0:
            return None
        try:
            data = yaml.safe_load(out)
        except yaml.YAMLError:
            return None
        if not isinstance(data, dict):
            return None
        return cast(NodeManifest, data)

    async def _get_event_commit_author(
        self, node_id: str, event_id: str
    ) -> dict[str, str]:
        """读取事件文件在节点分支上的 commit author（``name`` + ``email``）。

        使用 ``git log -1 --format=%an%n%ae <branch> -- .maf/events/<event-id>.json``
        获取最后修改该事件文件的 commit 的作者信息。

        只读：不修改任何分支或工作树。失败或事件文件不存在时抛
        :class:`NodeIdentityError`（``failure_reason=commit_author_read_failed``
        或 ``event_file_not_on_branch``）。
        """
        branch = build_node_branch_name(node_id)
        rel_path = build_event_file_path(event_id)
        rc, out, err = await self._git_cli.run(
            self._repository_path,
            ["log", "-1", "--format=%an%n%ae", branch, "--", rel_path],
            0,
        )
        if rc != 0:
            raise NodeIdentityError(
                f"failed to read commit author for event {event_id!r} on "
                f"branch {branch!r}: {err.strip()}",
                reason_code=ReasonCode.EVENT_NODE_IDENTITY_MISMATCH,
                context={
                    "node_id": node_id,
                    "event_id": event_id,
                    "branch": branch,
                    "failure_reason": "commit_author_read_failed",
                },
            )
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if len(lines) < 2:
            raise NodeIdentityError(
                f"event file {rel_path!r} not found on branch {branch!r}; "
                f"cannot verify commit author",
                reason_code=ReasonCode.EVENT_NODE_IDENTITY_MISMATCH,
                context={
                    "node_id": node_id,
                    "event_id": event_id,
                    "branch": branch,
                    "path": rel_path,
                    "failure_reason": "event_file_not_on_branch",
                },
            )
        return {"name": lines[0], "email": lines[1]}

    async def _list_node_branches(self) -> list[str]:
        """``git for-each-ref --format=%(refname:short) refs/heads/maf/node/``.

        Returns short branch names (e.g. ``maf/node/<node-id>``) sorted
        lexicographically. Read-only: does not modify any ref.
        """
        rc, out, _err = await self._git_cli.run(
            self._repository_path,
            [
                "for-each-ref",
                "--format=%(refname:short)",
                f"refs/heads/{NODE_BRANCH_PREFIX}",
            ],
            0,
        )
        if rc != 0:
            return []
        return sorted(line.strip() for line in out.splitlines() if line.strip())

    async def _is_diverged(self, old_commit: str, new_commit: str) -> bool:
        """Check whether ``old_commit`` is NOT an ancestor of ``new_commit``.

        Uses ``git rev-list --count <new>..<old>``: if any commit is reachable
        from ``old`` but not from ``new``, the history has diverged (force-push
        or rollback). Returns ``True`` when diverged or when ``old_commit``
        cannot be resolved.
        """
        rc, out, _err = await self._git_cli.run(
            self._repository_path,
            ["rev-list", "--count", f"{new_commit}..{old_commit}"],
            0,
        )
        if rc != 0:
            # old_commit does not exist or git error → treat as diverged.
            return True
        try:
            count = int(out.strip() or "0")
        except ValueError:
            return True
        return count > 0

    async def _list_changed_event_files(
        self, since_commit: str, head: str
    ) -> list[str]:
        """``git diff --name-only --diff-filter=A <since>..<head> -- .maf/events/``.

        Lists event files **added** between ``since_commit`` and ``head``.
        ``--diff-filter=A`` ensures only new files are returned (append-only
        events create one file per event, so modified files are not expected).
        """
        rc, out, _err = await self._git_cli.run(
            self._repository_path,
            [
                "diff",
                "--name-only",
                "--diff-filter=A",
                f"{since_commit}..{head}",
                "--",
                self._EVENT_DIR_PREFIX,
            ],
            0,
        )
        if rc != 0:
            # diff failed (e.g. bad ref) → return empty; caller handles via
            # diverged flag.
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def _read_file_from_branch(self, branch: str, path: str) -> str:
        """``git show <branch>:<path>``，只读读取分支上某文件的内容。

        不切换工作树、不修改任何文件。失败抛 :class:`UnsupportedOperationError`。
        """
        rc, out, err = await self._git_cli.run(
            self._repository_path,
            ["show", f"{branch}:{path}"],
            0,
        )
        if rc != 0:
            raise UnsupportedOperationError(
                f"failed to read {path!r} from {branch!r}: {err.strip()}",
                context={
                    "branch": branch,
                    "path": path,
                    "stderr": err,
                    "reason": "read_failed",
                },
            )
        return out

    async def _list_dir_from_branch(self, branch: str, prefix: str) -> list[str]:
        """``git ls-tree -r --name-only <branch> -- <prefix>``，列出分支上某目录所有文件。

        返回文件路径列表（相对仓库根，含 ``prefix``），按字典序排序。
        目录不存在或为空时返回空列表（不抛异常）。
        """
        rc, out, _err = await self._git_cli.run(
            self._repository_path,
            ["ls-tree", "-r", "--name-only", branch, "--", prefix],
            0,
        )
        if rc != 0:
            # ls-tree 对不存在路径返回非 0；视为空目录。
            return []
        paths = [line.strip() for line in out.splitlines() if line.strip()]
        return sorted(paths)

    async def _get_commit_timestamp(self, ref: str) -> str:
        """``git log -1 --format=%cI <ref>``，返回 commit 的 ISO 8601 时间戳。

        使用 ``%cI``（committer date, strict ISO 8601）确保跨平台一致。
        """
        rc, out, err = await self._git_cli.run(
            self._repository_path,
            ["log", "-1", "--format=%cI", ref],
            0,
        )
        if rc != 0:
            raise ExternalDependencyError(
                f"git log -1 --format=%cI {ref!r} failed: {err.strip()}",
                context={"ref": ref, "stderr": err, "returncode": rc},
                retryable=False,
            )
        return out.strip()

    async def _write_maf_protocol(self, project_id: str) -> None:
        """复制模板到 ``.maf/`` 并替换 ``project.yaml`` 中的 ``project_id`` 占位符。

        - 复制 ``templates_dir`` 下的 ``project.yaml``、``status.md``、``PROTOCOL.md``、
          ``schemas/``、``tasks/``、``nodes/``、``events/`` 到 ``.maf/``；
        - 用真实 ``project_id`` 替换 ``project.yaml`` 中的占位符
          ``replace-with-project-id``；
        - 用 :class:`SchemaLoader` 校验 ``project.yaml`` 符合 ``project-v1`` Schema。
        """
        repo_path = Path(self._repository_path)
        maf_dir = repo_path / _MAF_DIR_NAME
        # 清理潜在残留（防御性，正常情况下 maf/control 是空分支时无 .maf）。
        if maf_dir.exists():
            shutil.rmtree(maf_dir)
        maf_dir.mkdir(parents=True, exist_ok=False)

        # 复制模板内容（保留子目录结构：schemas/、tasks/、nodes/、events/）。
        template_items = [
            "project.yaml",
            "status.md",
            "PROTOCOL.md",
            "schemas",
            "tasks",
            "nodes",
            "events",
        ]
        for item in template_items:
            src = self._templates_dir / item
            if not src.exists():
                # 模板缺失视为配置错误，停止初始化（不覆盖）。
                raise ArgumentError(
                    f"git_coordination template missing: {src}",
                    context={"template_item": item},
                )
            dst = maf_dir / item
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

        # 替换 project.yaml 中的占位符。
        project_yaml_path = maf_dir / "project.yaml"
        text = project_yaml_path.read_text(encoding="utf-8")
        if _PROJECT_ID_PLACEHOLDER in text:
            text = text.replace(_PROJECT_ID_PLACEHOLDER, project_id)
        else:
            # 模板未含占位符：直接重写 project_id 行，确保写入真实值。
            # 这条路径仅在模板被改坏时触发，保证幂等产出永远携带真实 project_id。
            lines = text.splitlines()
            rewritten = []
            replaced = False
            for line in lines:
                stripped = line.lstrip()
                if stripped.startswith("project_id:"):
                    indent = line[: len(line) - len(stripped)]
                    rewritten.append(f"{indent}project_id: {project_id}")
                    replaced = True
                else:
                    rewritten.append(line)
            if not replaced:
                rewritten.append(f"project_id: {project_id}")
            text = "\n".join(rewritten) + "\n"
        project_yaml_path.write_text(text, encoding="utf-8")

        # Schema 校验：确保写入的 project.yaml 符合 project-v1 Schema。
        instance = YamlLoader.load(project_yaml_path)
        self._schema_loader.validate(
            SchemaRef(name="project", version=ProtocolVersion.latest().value),
            instance,
            source_file=project_yaml_path,
        )

    # ------------------------------------------------------------------ #
    # Git 工具：分支检查 / rev-parse / 当前分支记录与恢复
    # ------------------------------------------------------------------ #

    async def _branch_exists(self, name: str) -> bool:
        """``git show-ref --verify --quiet refs/heads/<name>``。"""
        rc, _out, _err = await self._git_cli.run(
            self._repository_path,
            ["show-ref", "--verify", "--quiet", f"refs/heads/{name}"],
            0,
        )
        return rc == 0

    async def _rev_parse(self, ref: str) -> str:
        """``git rev-parse <ref>``，返回去首尾空白的 commit hash。"""
        rc, out, err = await self._git_cli.run(
            self._repository_path,
            ["rev-parse", ref],
            0,
        )
        if rc != 0:
            raise ExternalDependencyError(
                f"git rev-parse {ref!r} failed: {err.strip()}",
                context={"ref": ref, "stderr": err, "returncode": rc},
                retryable=False,
            )
        return out.strip()

    async def _current_ref_for_restore(self) -> str:
        """记录当前分支名；detached HEAD 时记录 commit hash。

        返回值用于 :meth:`_restore_ref`：分支名优先用 ``git switch``，commit 用
        ``git switch --detach``。
        """
        rc, out, err = await self._git_cli.run(
            self._repository_path,
            ["rev-parse", "--abbrev-ref", "HEAD"],
            0,
        )
        if rc == 0:
            branch = out.strip()
            if branch and branch != "HEAD":
                return branch
        # detached HEAD：记录 commit hash。
        rc, out, err = await self._git_cli.run(
            self._repository_path,
            ["rev-parse", "HEAD"],
            0,
        )
        if rc != 0:
            raise ExternalDependencyError(
                f"cannot determine current HEAD for restore: {err.strip()}",
                context={"stderr": err, "returncode": rc},
                retryable=False,
            )
        return out.strip()

    async def _restore_ref(self, ref: str) -> None:
        """切回 :meth:`_current_ref_for_restore` 记录的分支/commit。

        失败仅记录日志，不抛异常：恢复失败意味着 main 工作树可能停留在
        ``maf/control``，但 control 分支本身已正确写入；调用方应人工排查。
        """
        if not ref:
            return
        # ref 是分支名 → ``git switch <branch>``；是 commit → ``git switch --detach``。
        is_branch = await self._branch_exists(ref)
        if is_branch:
            rc, _out, err = await self._git_cli.run(
                self._repository_path,
                ["switch", ref],
                0,
            )
        else:
            rc, _out, err = await self._git_cli.run(
                self._repository_path,
                ["switch", "--detach", ref],
                0,
            )
        if rc != 0:
            self._log.warning(
                "initialize_project_restore_failed",
                ref=ref,
                stderr=err,
            )

    async def _cleanup_maf_and_restore(self, original_ref: str) -> None:
        """失败回滚：删除工作树中的 ``.maf/`` 目录并切回原分支。

        用于 ``_create_control_branch_and_write_protocol`` 中任一步骤失败时
        恢复 main 工作树状态。``.maf/`` 在 maf/control 工作树上创建，
        ``git switch`` 不会清理未跟踪文件，因此需显式删除后再切回，
        避免污染 main 工作树（验收标准：初始化不修改 main 业务文件）。
        """
        maf_dir = Path(self._repository_path) / _MAF_DIR_NAME
        if maf_dir.exists():
            try:
                shutil.rmtree(maf_dir)
            except OSError as exc:
                self._log.warning(
                    "initialize_project_cleanup_maf_failed",
                    maf_dir=str(maf_dir),
                    error=str(exc),
                )
        if original_ref:
            await self._restore_ref(original_ref)

    # ------------------------------------------------------------------ #
    # 模板目录解析
    # ------------------------------------------------------------------ #

    @staticmethod
    def _default_templates_dir() -> Path:
        """返回 ``templates/git_coordination`` 的项目相对路径。

        本文件位于 ``apps/server/src/maf_server/modules/git_coordination/service.py``，
        向上 6 级到项目根（比 ``git_coordination/schemas.py`` 多一级 ``modules/``），
        再进入 ``templates/git_coordination``。
        """
        return Path(__file__).resolve().parents[6] / "templates" / "git_coordination"


__all__ = [
    "AssignmentEpochError",
    "AssignmentEpochStaleError",
    "ClaimDecision",
    "CoordinationSnapshot",
    "DiscoveredEvents",
    "EventDiscoveryService",
    "GitCoordinationService",
    "GitCoordinationStateService",
    "InvalidEventEntry",
    "LocalGitCoordinationService",
    "NodeIdentity",
    "NodeIdentityError",
    "ProcessResult",
    "TaskAllocator",
    "check_assignment_epoch",
    "validate_assignment_epoch",
]
