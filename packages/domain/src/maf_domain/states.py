"""Task lifecycle state machine, aligned with the GitHub distributed
collaboration protocol §5.

The state machine is a pure Python component: states are an ``Enum``, transitions
are driven by ``TaskEvent`` values, and ``Actor`` distinguishes authoritative
scheduler writes from node-reported requests. Per protocol §5:

- 中央调度器是唯一写者，节点不能直接写权威状态；
- ``DONE`` 只能在评审、测试、验收和合并完成后写入；
- 终态（``DONE``、``FAILED``、``CANCELLED``）不允许退回 ``IN_PROGRESS``。

Git push、SQLite 投影和节点事件分支消费由 ``apps.server.modules.git_coordination``
的服务层负责；本模块只包含领域状态机，不依赖 FastAPI、SQLAlchemy、LangGraph、
Docker 或模型 SDK。
"""

from __future__ import annotations

from enum import Enum
from typing import Final, NamedTuple

from maf_domain.errors import UnsupportedOperationError


# --------------------------------------------------------------------------- #
# 乐观锁版本约定（TASK-008）
# --------------------------------------------------------------------------- #
#
# 与《多 Agent 协同工具系统设计文档》6.1 节通用字段表一致：业务表统一包含
# ``version_no INTEGER NOT NULL DEFAULT 1`` 列作为乐观锁版本号。Repository
# 在 ``UnitOfWork`` 事务内更新聚合根时，调用
# ``maf_server.core.unit_of_work.update_with_expected_version`` 执行
# ``UPDATE ... WHERE version_no = <expected_version>``，影响行数 0 抛
# ``VersionConflictError``（API 层映射 HTTP 409）。

#: 乐观锁期望版本号类型别名。调用方在更新前读取聚合根的 ``version_no`` 字段
#: 作为 ``expected_version`` 传入更新辅助函数；类型为 ``int`` 与 SQLite
#: ``INTEGER`` 列对应。
ExpectedVersion = int

#: 业务表乐观锁列名，与设计文档 6.1 节通用字段表 ``version_no`` 一致，
#: 跨表稳定，禁止重命名。
VERSION_COLUMN_DEFAULT: Final[str] = "version_no"

#: 新建资源的初始版本号，与通用字段表 ``DEFAULT 1`` 一致。
VERSION_INITIAL: Final[int] = 1


class TaskState(str, Enum):
    """任务生命周期状态，取值与 ``task-v1.schema.json`` 的 ``status`` 枚举及
    《GitHub 分布式协作协议》第 5 节状态机一致，跨版本稳定。
    """

    PLANNED = "PLANNED"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    SUBMITTED = "SUBMITTED"
    REVIEWING = "REVIEWING"
    REWORK_REQUIRED = "REWORK_REQUIRED"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @classmethod
    def terminal_states(cls) -> frozenset["TaskState"]:
        """协议 §5 定义的无出向转换状态：DONE/FAILED/CANCELLED。"""
        return frozenset({cls.DONE, cls.FAILED, cls.CANCELLED})

    def is_terminal(self) -> bool:
        """是否为终态。终态无任何出向转换，不接受 ``PROGRESS_REPORTED`` 等回退。"""
        return self in type(self).terminal_states()


class Actor(str, Enum):
    """状态转换发起方。

    协议 §5 规定中央调度器是唯一权威写者，节点只能向
    ``maf/node/<node-id>`` 追加事件由调度器消费。``Actor`` 用于
    ``TaskStateMachine.transition`` 拒绝节点直接写入权威状态
    （``ASSIGNED``、``REVIEWING``、``DONE``）。
    """

    SCHEDULER = "SCHEDULER"
    NODE = "NODE"


class TaskEvent(str, Enum):
    """驱动 ``TaskState`` 转换的事件。

    节点追加的 ``CoordinationEvent`` 类型在 ``GitCoordinationService`` 内映射
    为 ``TaskEvent``；调度器内部事件（如 ``DEPENDENCIES_RESOLVED``、
    ``REVIEW_APPROVED``）没有对应的 ``CoordinationEvent``，因为这些权威转换
    由中央调度器独占触发。
    """

    # 依赖与分发
    DEPENDENCIES_RESOLVED = "DEPENDENCIES_RESOLVED"
    CLAIM_GRANTED = "CLAIM_GRANTED"
    PROGRESS_REPORTED = "PROGRESS_REPORTED"
    BLOCKED_REPORTED = "BLOCKED_REPORTED"
    BLOCK_RESOLVED = "BLOCK_RESOLVED"
    # 提交与评审
    SUBMISSION_ACCEPTED = "SUBMISSION_ACCEPTED"
    REVIEW_STARTED = "REVIEW_STARTED"
    REVIEW_APPROVED = "REVIEW_APPROVED"
    REVIEW_REJECTED = "REVIEW_REJECTED"
    # 生命周期
    LEASE_EXPIRED = "LEASE_EXPIRED"
    REASSIGNED = "REASSIGNED"
    REQUEUED = "REQUEUED"
    WORK_ABANDONED = "WORK_ABANDONED"
    TASK_FAILED = "TASK_FAILED"


# 合法转换表：(current_state, event) -> target_state
# 仅覆盖协议 §5 主路径与分支转换；``WORK_ABANDONED`` 与 ``TASK_FAILED`` 适用于
# 任意非终态，在 ``TaskStateMachine.transition`` 内部直接处理，不入此表。
_TRANSITIONS: dict[tuple[TaskState, TaskEvent], TaskState] = {
    # 主路径：PLANNED → READY → ASSIGNED → IN_PROGRESS → SUBMITTED → REVIEWING → DONE
    (TaskState.PLANNED, TaskEvent.DEPENDENCIES_RESOLVED): TaskState.READY,
    (TaskState.READY, TaskEvent.CLAIM_GRANTED): TaskState.ASSIGNED,
    (TaskState.ASSIGNED, TaskEvent.PROGRESS_REPORTED): TaskState.IN_PROGRESS,
    (TaskState.IN_PROGRESS, TaskEvent.SUBMISSION_ACCEPTED): TaskState.SUBMITTED,
    (TaskState.SUBMITTED, TaskEvent.REVIEW_STARTED): TaskState.REVIEWING,
    (TaskState.REVIEWING, TaskEvent.REVIEW_APPROVED): TaskState.DONE,

    # 阻塞分支：active worker 状态可报告阻塞
    (TaskState.ASSIGNED, TaskEvent.BLOCKED_REPORTED): TaskState.BLOCKED,
    (TaskState.IN_PROGRESS, TaskEvent.BLOCKED_REPORTED): TaskState.BLOCKED,
    (TaskState.REWORK_REQUIRED, TaskEvent.BLOCKED_REPORTED): TaskState.BLOCKED,
    (TaskState.BLOCKED, TaskEvent.BLOCK_RESOLVED): TaskState.IN_PROGRESS,

    # 返工回路：评审拒绝 → 返工 → 进行中 → 重新提交
    (TaskState.REVIEWING, TaskEvent.REVIEW_REJECTED): TaskState.REWORK_REQUIRED,
    (TaskState.SUBMITTED, TaskEvent.REVIEW_REJECTED): TaskState.REWORK_REQUIRED,
    (TaskState.REWORK_REQUIRED, TaskEvent.PROGRESS_REPORTED): TaskState.IN_PROGRESS,
    (TaskState.REWORK_REQUIRED, TaskEvent.SUBMISSION_ACCEPTED): TaskState.SUBMITTED,

    # 租约过期与重新分配
    (TaskState.ASSIGNED, TaskEvent.LEASE_EXPIRED): TaskState.LEASE_EXPIRED,
    (TaskState.IN_PROGRESS, TaskEvent.LEASE_EXPIRED): TaskState.LEASE_EXPIRED,
    (TaskState.BLOCKED, TaskEvent.LEASE_EXPIRED): TaskState.LEASE_EXPIRED,
    (TaskState.REWORK_REQUIRED, TaskEvent.LEASE_EXPIRED): TaskState.LEASE_EXPIRED,
    (TaskState.LEASE_EXPIRED, TaskEvent.REASSIGNED): TaskState.ASSIGNED,
    (TaskState.LEASE_EXPIRED, TaskEvent.REQUEUED): TaskState.READY,
}


# 节点不能直接写入的权威目标状态（协议 §5：节点不能直接设置 DONE、ASSIGNED、REVIEWING）。
_SCHEDULER_ONLY_TARGETS: frozenset[TaskState] = frozenset({
    TaskState.ASSIGNED,
    TaskState.REVIEWING,
    TaskState.DONE,
})


class TaskStateTransition(NamedTuple):
    """``TaskEvent`` 应用到 ``TaskState`` 的结果。

    ``new_version`` 为 ``current_version + 1``，调用方将其作为任务的乐观并发
    版本号持久化（与 ``task-v1.schema.json`` 中 ``version`` 字段对应）。
    """

    new_state: TaskState
    new_version: int


class TaskStateMachine:
    """``TaskState`` 生命周期状态机。

    校验顺序：

    1. ``current`` 不能是终态（``DONE``/``FAILED``/``CANCELLED`` 无出向转换，
       终态不退回 ``IN_PROGRESS``）；
    2. ``event`` 必须在 ``_TRANSITIONS`` 中对 ``current`` 合法，或为
       ``WORK_ABANDONED``/``TASK_FAILED`` 兜底事件（任意非终态均可）；
    3. ``actor`` 必须有权限写入目标状态（``NODE`` 不能直接写
       ``ASSIGNED``/``REVIEWING``/``DONE``）。

    成功返回 ``TaskStateTransition``（新状态 + ``current_version + 1``）；
    失败抛 ``UnsupportedOperationError``，``context`` 携带 ``current_state``、
    ``event``、``actor`` 与（若可确定）``target_state`` 便于审计与重试决策。
    """

    def transition(
        self,
        current: TaskState,
        event: TaskEvent,
        *,
        actor: Actor = Actor.SCHEDULER,
        current_version: int = 1,
    ) -> TaskStateTransition:
        """应用 ``event`` 到 ``current``，返回新状态和递增后的版本号。

        :raises UnsupportedOperationError: 转换非法或 ``actor`` 不被允许。
        """
        if current.is_terminal():
            raise UnsupportedOperationError(
                f"任务状态 '{current.value}' 是终态，不允许转换",
                context={
                    "current_state": current.value,
                    "event": event.value,
                    "actor": actor.value,
                },
            )
        target = self._resolve_target(current, event)
        if actor is Actor.NODE and target in _SCHEDULER_ONLY_TARGETS:
            raise UnsupportedOperationError(
                f"节点不能直接设置状态 '{target.value}'，须由中央调度器写入",
                context={
                    "current_state": current.value,
                    "event": event.value,
                    "target_state": target.value,
                    "actor": actor.value,
                },
            )
        return TaskStateTransition(new_state=target, new_version=current_version + 1)

    def can_transition(
        self,
        current: TaskState,
        event: TaskEvent,
        *,
        actor: Actor = Actor.SCHEDULER,
        current_version: int = 1,
    ) -> bool:
        """检查转换是否合法，不抛异常。"""
        try:
            self.transition(
                current,
                event,
                actor=actor,
                current_version=current_version,
            )
        except UnsupportedOperationError:
            return False
        return True

    def allowed_events(
        self,
        current: TaskState,
        *,
        actor: Actor = Actor.SCHEDULER,
    ) -> list[TaskEvent]:
        """返回 ``current`` 状态下 ``actor`` 可触发的事件列表。

        终态返回空列表，保证不会从终态退回 ``IN_PROGRESS`` 等中间态。
        """
        if current.is_terminal():
            return []
        events: list[TaskEvent] = []
        for event in TaskEvent:
            if self.can_transition(current, event, actor=actor):
                events.append(event)
        return events

    @staticmethod
    def _resolve_target(current: TaskState, event: TaskEvent) -> TaskState:
        """解析 (current, event) 的目标状态。兜底事件固定指向对应终态。"""
        if event is TaskEvent.WORK_ABANDONED:
            return TaskState.CANCELLED
        if event is TaskEvent.TASK_FAILED:
            return TaskState.FAILED
        target = _TRANSITIONS.get((current, event))
        if target is None:
            raise UnsupportedOperationError(
                f"任务状态 '{current.value}' 不允许事件 '{event.value}'",
                context={
                    "current_state": current.value,
                    "event": event.value,
                },
            )
        return target


__all__ = [
    "Actor",
    "ExpectedVersion",
    "TaskEvent",
    "TaskState",
    "TaskStateMachine",
    "TaskStateTransition",
    "VERSION_COLUMN_DEFAULT",
    "VERSION_INITIAL",
]
