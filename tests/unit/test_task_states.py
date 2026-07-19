"""TASK-022 单元测试：任务状态转换引擎。

验收标准：
1. 所有允许/禁止转换有参数化测试。
2. 节点不能直接设置 DONE、ASSIGNED 或 REVIEWING。
3. 终态不会退回 IN_PROGRESS。

测试范围：
- ``packages/domain/src/maf_domain/states.py``：``TaskState``、``TaskEvent``、
  ``Actor``、``TaskStateMachine.transition``、``TaskStateTransition``。
- ``apps/server/src/maf_server/modules/git_coordination/service.py``：
  ``GitCoordinationStateService.apply_task_event`` 服务层包装。
"""

from __future__ import annotations

import pytest

from maf_domain.errors import ErrorCode, UnsupportedOperationError
from maf_domain.states import (
    Actor,
    TaskEvent,
    TaskState,
    TaskStateMachine,
    TaskStateTransition,
)
from maf_server.modules.git_coordination.service import GitCoordinationStateService


# --------------------------------------------------------------------------- #
# 协议常量与合法转换集
# --------------------------------------------------------------------------- #


# 与 task-v1.schema.json 的 status 枚举一致
EXPECTED_STATE_VALUES: frozenset[str] = frozenset({
    "PLANNED", "READY", "ASSIGNED", "IN_PROGRESS", "BLOCKED", "SUBMITTED",
    "REVIEWING", "REWORK_REQUIRED", "LEASE_EXPIRED", "DONE", "FAILED", "CANCELLED",
})

# 协议 §5 主路径与分支转换（不含兜底事件）
LEGAL_SPECIFIC_TRANSITIONS: list[tuple[TaskState, TaskEvent, TaskState]] = [
    # 主路径
    (TaskState.PLANNED, TaskEvent.DEPENDENCIES_RESOLVED, TaskState.READY),
    (TaskState.READY, TaskEvent.CLAIM_GRANTED, TaskState.ASSIGNED),
    (TaskState.ASSIGNED, TaskEvent.PROGRESS_REPORTED, TaskState.IN_PROGRESS),
    (TaskState.IN_PROGRESS, TaskEvent.SUBMISSION_ACCEPTED, TaskState.SUBMITTED),
    (TaskState.SUBMITTED, TaskEvent.REVIEW_STARTED, TaskState.REVIEWING),
    (TaskState.REVIEWING, TaskEvent.REVIEW_APPROVED, TaskState.DONE),
    # 阻塞分支
    (TaskState.ASSIGNED, TaskEvent.BLOCKED_REPORTED, TaskState.BLOCKED),
    (TaskState.IN_PROGRESS, TaskEvent.BLOCKED_REPORTED, TaskState.BLOCKED),
    (TaskState.REWORK_REQUIRED, TaskEvent.BLOCKED_REPORTED, TaskState.BLOCKED),
    (TaskState.BLOCKED, TaskEvent.BLOCK_RESOLVED, TaskState.IN_PROGRESS),
    # 返工回路
    (TaskState.REVIEWING, TaskEvent.REVIEW_REJECTED, TaskState.REWORK_REQUIRED),
    (TaskState.SUBMITTED, TaskEvent.REVIEW_REJECTED, TaskState.REWORK_REQUIRED),
    (TaskState.REWORK_REQUIRED, TaskEvent.PROGRESS_REPORTED, TaskState.IN_PROGRESS),
    (TaskState.REWORK_REQUIRED, TaskEvent.SUBMISSION_ACCEPTED, TaskState.SUBMITTED),
    # 租约过期与重新分配
    (TaskState.ASSIGNED, TaskEvent.LEASE_EXPIRED, TaskState.LEASE_EXPIRED),
    (TaskState.IN_PROGRESS, TaskEvent.LEASE_EXPIRED, TaskState.LEASE_EXPIRED),
    (TaskState.BLOCKED, TaskEvent.LEASE_EXPIRED, TaskState.LEASE_EXPIRED),
    (TaskState.REWORK_REQUIRED, TaskEvent.LEASE_EXPIRED, TaskState.LEASE_EXPIRED),
    (TaskState.LEASE_EXPIRED, TaskEvent.REASSIGNED, TaskState.ASSIGNED),
    (TaskState.LEASE_EXPIRED, TaskEvent.REQUEUED, TaskState.READY),
]

# 兜底事件：从任意非终态都能转换到对应终态
CATCH_ALL_EVENTS: list[tuple[TaskEvent, TaskState]] = [
    (TaskEvent.WORK_ABANDONED, TaskState.CANCELLED),
    (TaskEvent.TASK_FAILED, TaskState.FAILED),
]

# 节点不能直接写入的权威目标状态（协议 §5）
SCHEDULER_ONLY_TARGETS: frozenset[TaskState] = frozenset({
    TaskState.ASSIGNED,
    TaskState.REVIEWING,
    TaskState.DONE,
})

NON_TERMINAL_STATES: list[TaskState] = [s for s in TaskState if not s.is_terminal()]
TERMINAL_STATES: list[TaskState] = list(TaskState.terminal_states())


def _legal_pairs() -> set[tuple[TaskState, TaskEvent]]:
    """计算所有合法 (state, event) 对，包含主路径与兜底事件。"""
    pairs: set[tuple[TaskState, TaskEvent]] = {
        (state, event) for state, event, _ in LEGAL_SPECIFIC_TRANSITIONS
    }
    for state in NON_TERMINAL_STATES:
        for event, _ in CATCH_ALL_EVENTS:
            pairs.add((state, event))
    return pairs


LEGAL_PAIRS: set[tuple[TaskState, TaskEvent]] = _legal_pairs()


def _all_state_event_pairs() -> list[tuple[TaskState, TaskEvent]]:
    return [(s, e) for s in TaskState for e in TaskEvent]


# 终态 × 全部事件：均应拒绝
TERMINAL_PAIRS: list[tuple[TaskState, TaskEvent]] = [
    (s, e) for s, e in _all_state_event_pairs() if s.is_terminal()
]

# 非终态 × 非法事件：均应抛错
ILLEGAL_NON_TERMINAL_PAIRS: list[tuple[TaskState, TaskEvent]] = [
    (s, e) for s, e in _all_state_event_pairs()
    if not s.is_terminal() and (s, e) not in LEGAL_PAIRS
]


# --------------------------------------------------------------------------- #
# 验收标准 0：状态枚举与协议一致
# --------------------------------------------------------------------------- #


def test_state_enum_values_match_schema_and_protocol() -> None:
    """TaskState 取值与 task-v1.schema.json status 枚举和协议 §5 一致。"""
    values = {member.value for member in TaskState}
    assert values == EXPECTED_STATE_VALUES


def test_state_enum_values_match_member_names() -> None:
    """TaskState.value 与成员名一致，保证跨版本稳定（与 ReasonCode 风格一致）。"""
    for member in TaskState:
        assert member.value == member.name


def test_state_enum_has_twelve_states() -> None:
    """task-v1.schema.json 共声明 12 个 status 取值。"""
    assert len(list(TaskState)) == 12


def test_terminal_states_match_protocol() -> None:
    """终态集合为 DONE/FAILED/CANCELLED，与协议 §5 一致。"""
    assert TaskState.terminal_states() == frozenset({
        TaskState.DONE, TaskState.FAILED, TaskState.CANCELLED,
    })


def test_is_terminal_predicate_consistent_with_terminal_states() -> None:
    """is_terminal 与 terminal_states 一致。"""
    for state in TaskState:
        assert state.is_terminal() == (state in TaskState.terminal_states())


def test_actor_enum_values() -> None:
    """Actor 区分中央调度器与节点。"""
    assert Actor.SCHEDULER.value == "SCHEDULER"
    assert Actor.NODE.value == "NODE"
    assert len(list(Actor)) == 2


# --------------------------------------------------------------------------- #
# 验收标准 1：所有允许/禁止转换有参数化测试
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("current", "event", "expected"),
    LEGAL_SPECIFIC_TRANSITIONS,
    ids=[f"{c.value}+{e.value}->{t.value}" for c, e, t in LEGAL_SPECIFIC_TRANSITIONS],
)
def test_legal_specific_transition_returns_target(
    current: TaskState, event: TaskEvent, expected: TaskState
) -> None:
    """转换表中的每条合法转换应成功并返回目标状态。"""
    sm = TaskStateMachine()
    result = sm.transition(current, event)
    assert result.new_state == expected


@pytest.mark.parametrize(
    "state",
    NON_TERMINAL_STATES,
    ids=[s.value for s in NON_TERMINAL_STATES],
)
@pytest.mark.parametrize(
    ("event", "target"),
    CATCH_ALL_EVENTS,
    ids=[e.value for e, _ in CATCH_ALL_EVENTS],
)
def test_catch_all_event_from_non_terminal_state(
    state: TaskState, event: TaskEvent, target: TaskState
) -> None:
    """WORK_ABANDONED/TASK_FAILED 从任意非终态都能转换到对应终态。"""
    sm = TaskStateMachine()
    result = sm.transition(state, event)
    assert result.new_state == target


@pytest.mark.parametrize(
    ("state", "event"),
    TERMINAL_PAIRS,
    ids=[f"{s.value}+{e.value}" for s, e in TERMINAL_PAIRS],
)
def test_terminal_state_rejects_all_events(
    state: TaskState, event: TaskEvent
) -> None:
    """终态对所有事件都拒绝；包括 PROGRESS_REPORTED 等可能进入 IN_PROGRESS 的事件。"""
    sm = TaskStateMachine()
    with pytest.raises(UnsupportedOperationError) as exc_info:
        sm.transition(state, event)
    err = exc_info.value
    assert err.error_code == ErrorCode.UNSUPPORTED_OPERATION
    assert err.context["current_state"] == state.value
    assert "终态" in err.message


@pytest.mark.parametrize(
    ("state", "event"),
    ILLEGAL_NON_TERMINAL_PAIRS,
    ids=[f"{s.value}+{e.value}" for s, e in ILLEGAL_NON_TERMINAL_PAIRS],
)
def test_illegal_non_terminal_transition_raises(
    state: TaskState, event: TaskEvent
) -> None:
    """非终态下未在转换表中的事件应抛 UnsupportedOperationError，携带 state/event 上下文。"""
    sm = TaskStateMachine()
    with pytest.raises(UnsupportedOperationError) as exc_info:
        sm.transition(state, event)
    err = exc_info.value
    assert err.error_code == ErrorCode.UNSUPPORTED_OPERATION
    assert err.context["current_state"] == state.value
    assert err.context["event"] == event.value


# --------------------------------------------------------------------------- #
# 验收标准 2：节点不能直接设置 DONE、ASSIGNED 或 REVIEWING
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("current", "event", "forbidden_target"),
    [
        (TaskState.READY, TaskEvent.CLAIM_GRANTED, TaskState.ASSIGNED),
        (TaskState.SUBMITTED, TaskEvent.REVIEW_STARTED, TaskState.REVIEWING),
        (TaskState.REVIEWING, TaskEvent.REVIEW_APPROVED, TaskState.DONE),
    ],
    ids=[
        "node-cannot-set-ASSIGNED",
        "node-cannot-set-REVIEWING",
        "node-cannot-set-DONE",
    ],
)
def test_node_actor_cannot_set_scheduler_only_targets(
    current: TaskState, event: TaskEvent, forbidden_target: TaskState
) -> None:
    """NODE actor 不能直接设置 ASSIGNED/REVIEWING/DONE，须由 SCHEDULER 写入。

    覆盖协议 §5 与 TASK-022 验收标准 2。
    """
    sm = TaskStateMachine()
    # SCHEDULER 可以执行该转换
    scheduler_result = sm.transition(current, event, actor=Actor.SCHEDULER)
    assert scheduler_result.new_state == forbidden_target
    # NODE 不能执行该转换
    with pytest.raises(UnsupportedOperationError) as exc_info:
        sm.transition(current, event, actor=Actor.NODE)
    err = exc_info.value
    assert err.error_code == ErrorCode.UNSUPPORTED_OPERATION
    assert err.context["target_state"] == forbidden_target.value
    assert err.context["actor"] == Actor.NODE.value
    assert err.context["current_state"] == current.value


@pytest.mark.parametrize(
    ("current", "event", "target"),
    [
        (c, e, t)
        for c, e, t in LEGAL_SPECIFIC_TRANSITIONS
        if t not in SCHEDULER_ONLY_TARGETS
    ],
    ids=[
        f"node-allowed-{c.value}+{e.value}"
        for c, e, t in LEGAL_SPECIFIC_TRANSITIONS
        if t not in SCHEDULER_ONLY_TARGETS
    ],
)
def test_node_actor_can_trigger_non_scheduler_only_transitions(
    current: TaskState, event: TaskEvent, target: TaskState
) -> None:
    """NODE actor 可触发目标非 SCHEDULER_ONLY 的合法转换（如报告进度、阻塞）。"""
    sm = TaskStateMachine()
    result = sm.transition(current, event, actor=Actor.NODE)
    assert result.new_state == target


@pytest.mark.parametrize(
    ("state", "event", "target"),
    [(s, e, t) for e, t in CATCH_ALL_EVENTS for s in NON_TERMINAL_STATES],
    ids=[
        f"node-catch-all-{s.value}+{e.value}"
        for e, t in CATCH_ALL_EVENTS for s in NON_TERMINAL_STATES
    ],
)
def test_node_actor_can_trigger_catch_all_events(
    state: TaskState, event: TaskEvent, target: TaskState
) -> None:
    """NODE actor 可触发 WORK_ABANDONED/TASK_FAILED 兜底事件（目标非 SCHEDULER_ONLY）。"""
    sm = TaskStateMachine()
    result = sm.transition(state, event, actor=Actor.NODE)
    assert result.new_state == target


# --------------------------------------------------------------------------- #
# 验收标准 3：终态不会退回 IN_PROGRESS
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("terminal", TERMINAL_STATES, ids=[s.value for s in TERMINAL_STATES])
def test_terminal_does_not_revert_to_in_progress_via_progress_reported(
    terminal: TaskState,
) -> None:
    """终态不能通过 PROGRESS_REPORTED 退回 IN_PROGRESS。"""
    sm = TaskStateMachine()
    with pytest.raises(UnsupportedOperationError):
        sm.transition(terminal, TaskEvent.PROGRESS_REPORTED)


@pytest.mark.parametrize("terminal", TERMINAL_STATES, ids=[s.value for s in TERMINAL_STATES])
def test_terminal_does_not_revert_to_in_progress_via_block_resolved(
    terminal: TaskState,
) -> None:
    """终态不能通过 BLOCK_RESOLVED 退回 IN_PROGRESS。"""
    sm = TaskStateMachine()
    with pytest.raises(UnsupportedOperationError):
        sm.transition(terminal, TaskEvent.BLOCK_RESOLVED)


@pytest.mark.parametrize("terminal", TERMINAL_STATES, ids=[s.value for s in TERMINAL_STATES])
def test_terminal_allowed_events_is_empty(terminal: TaskState) -> None:
    """终态 allowed_events 返回空列表，没有出向转换。"""
    sm = TaskStateMachine()
    assert sm.allowed_events(terminal) == []
    assert sm.allowed_events(terminal, actor=Actor.NODE) == []


# --------------------------------------------------------------------------- #
# 版本递增
# --------------------------------------------------------------------------- #


def test_transition_increments_version() -> None:
    """每次成功转换将版本号 +1。"""
    sm = TaskStateMachine()
    result = sm.transition(
        TaskState.PLANNED,
        TaskEvent.DEPENDENCIES_RESOLVED,
        current_version=5,
    )
    assert result.new_version == 6
    assert result.new_state == TaskState.READY


def test_transition_default_version_starts_from_one() -> None:
    """未提供 current_version 时默认从 1 开始递增到 2。"""
    sm = TaskStateMachine()
    result = sm.transition(TaskState.PLANNED, TaskEvent.DEPENDENCIES_RESOLVED)
    assert result.new_version == 2


def test_transition_independent_of_actor_for_version() -> None:
    """版本递增不受 actor 影响。"""
    sm = TaskStateMachine()
    node_result = sm.transition(
        TaskState.ASSIGNED,
        TaskEvent.PROGRESS_REPORTED,
        actor=Actor.NODE,
        current_version=10,
    )
    assert node_result.new_version == 11
    scheduler_result = sm.transition(
        TaskState.ASSIGNED,
        TaskEvent.PROGRESS_REPORTED,
        actor=Actor.SCHEDULER,
        current_version=10,
    )
    assert scheduler_result.new_version == 11


# --------------------------------------------------------------------------- #
# can_transition / allowed_events
# --------------------------------------------------------------------------- #


def test_can_transition_true_for_legal() -> None:
    """can_transition 对合法转换返回 True。"""
    sm = TaskStateMachine()
    assert sm.can_transition(TaskState.PLANNED, TaskEvent.DEPENDENCIES_RESOLVED)
    assert sm.can_transition(TaskState.REVIEWING, TaskEvent.REVIEW_APPROVED)


def test_can_transition_false_for_illegal() -> None:
    """can_transition 对非法转换返回 False，不抛异常。"""
    sm = TaskStateMachine()
    assert not sm.can_transition(TaskState.DONE, TaskEvent.PROGRESS_REPORTED)
    assert not sm.can_transition(TaskState.PLANNED, TaskEvent.REVIEW_APPROVED)
    assert not sm.can_transition(TaskState.READY, TaskEvent.REVIEW_APPROVED)


def test_can_transition_false_for_node_on_scheduler_only_target() -> None:
    """can_transition 对 NODE actor 触发 SCHEDULER_ONLY 目标返回 False。"""
    sm = TaskStateMachine()
    assert not sm.can_transition(
        TaskState.READY, TaskEvent.CLAIM_GRANTED, actor=Actor.NODE
    )
    assert sm.can_transition(
        TaskState.READY, TaskEvent.CLAIM_GRANTED, actor=Actor.SCHEDULER
    )


def test_allowed_events_for_planned_includes_dependencies_resolved_and_catch_alls() -> None:
    """PLANNED 状态下 SCHEDULER 可触发的事件包含 DEPENDENCIES_RESOLVED 与兜底事件。"""
    sm = TaskStateMachine()
    events = sm.allowed_events(TaskState.PLANNED)
    assert TaskEvent.DEPENDENCIES_RESOLVED in events
    assert TaskEvent.WORK_ABANDONED in events
    assert TaskEvent.TASK_FAILED in events
    # 不能直接进入 IN_PROGRESS（无 PROGRESS_REPORTED）
    assert TaskEvent.PROGRESS_REPORTED not in events
    # 不能直接进入 ASSIGNED（READY 才行）
    assert TaskEvent.CLAIM_GRANTED not in events


def test_allowed_events_excludes_scheduler_only_for_node_actor() -> None:
    """NODE actor 的 allowed_events 不包含导致 ASSIGNED/REVIEWING/DONE 的事件。"""
    sm = TaskStateMachine()
    # READY → ASSIGNED via CLAIM_GRANTED，节点不能直接触发
    node_events_ready = sm.allowed_events(TaskState.READY, actor=Actor.NODE)
    assert TaskEvent.CLAIM_GRANTED not in node_events_ready
    scheduler_events_ready = sm.allowed_events(TaskState.READY, actor=Actor.SCHEDULER)
    assert TaskEvent.CLAIM_GRANTED in scheduler_events_ready
    # SUBMITTED → REVIEWING via REVIEW_STARTED
    node_events_submitted = sm.allowed_events(TaskState.SUBMITTED, actor=Actor.NODE)
    assert TaskEvent.REVIEW_STARTED not in node_events_submitted
    # REVIEWING → DONE via REVIEW_APPROVED
    node_events_reviewing = sm.allowed_events(TaskState.REVIEWING, actor=Actor.NODE)
    assert TaskEvent.REVIEW_APPROVED not in node_events_reviewing


# --------------------------------------------------------------------------- #
# TaskStateTransition NamedTuple 与错误上下文稳定性
# --------------------------------------------------------------------------- #


def test_task_state_transition_namedtuple_fields() -> None:
    """TaskStateTransition 是 NamedTuple，含 new_state 和 new_version 字段。"""
    result = TaskStateMachine().transition(
        TaskState.PLANNED,
        TaskEvent.DEPENDENCIES_RESOLVED,
        current_version=3,
    )
    assert isinstance(result, TaskStateTransition)
    assert result.new_state == TaskState.READY
    assert result.new_version == 4
    # NamedTuple 支持解包
    state, version = result
    assert state == TaskState.READY
    assert version == 4


def test_illegal_transition_error_context_stability() -> None:
    """非法转换错误 context 必须包含 current_state 和 event，便于审计。"""
    sm = TaskStateMachine()
    with pytest.raises(UnsupportedOperationError) as exc_info:
        sm.transition(TaskState.PLANNED, TaskEvent.REVIEW_APPROVED)
    err = exc_info.value
    assert err.context["current_state"] == "PLANNED"
    assert err.context["event"] == "REVIEW_APPROVED"
    assert err.retryable is False


def test_node_forbidden_transition_error_context_includes_target_and_actor() -> None:
    """节点尝试设置 SCHEDULER_ONLY 状态时，context 包含 target_state 和 actor。"""
    sm = TaskStateMachine()
    with pytest.raises(UnsupportedOperationError) as exc_info:
        sm.transition(TaskState.READY, TaskEvent.CLAIM_GRANTED, actor=Actor.NODE)
    err = exc_info.value
    assert err.context["target_state"] == "ASSIGNED"
    assert err.context["actor"] == "NODE"
    assert err.context["current_state"] == "READY"
    assert err.context["event"] == "CLAIM_GRANTED"


def test_terminal_state_error_context_includes_actor() -> None:
    """终态拒绝转换时 context 包含 actor，便于审计谁尝试退回。"""
    sm = TaskStateMachine()
    with pytest.raises(UnsupportedOperationError) as exc_info:
        sm.transition(TaskState.DONE, TaskEvent.PROGRESS_REPORTED, actor=Actor.NODE)
    err = exc_info.value
    assert err.context["actor"] == "NODE"
    assert err.context["current_state"] == "DONE"


# --------------------------------------------------------------------------- #
# GitCoordinationStateService 服务层包装
# --------------------------------------------------------------------------- #


def test_state_service_delegates_to_state_machine() -> None:
    """GitCoordinationStateService.apply_task_event 委托给 TaskStateMachine。"""
    service = GitCoordinationStateService()
    result = service.apply_task_event(
        TaskState.READY,
        TaskEvent.CLAIM_GRANTED,
        actor=Actor.SCHEDULER,
        current_version=2,
    )
    assert result.new_state == TaskState.ASSIGNED
    assert result.new_version == 3


def test_state_service_propagates_illegal_transition_error() -> None:
    """Service 层包装对非法转换抛 UnsupportedOperationError。"""
    service = GitCoordinationStateService()
    with pytest.raises(UnsupportedOperationError):
        service.apply_task_event(TaskState.DONE, TaskEvent.PROGRESS_REPORTED)


def test_state_service_rejects_node_actor_for_scheduler_only_targets() -> None:
    """Service 层包装在 NODE actor 尝试设置 SCHEDULER_ONLY 状态时拒绝。"""
    service = GitCoordinationStateService()
    with pytest.raises(UnsupportedOperationError):
        service.apply_task_event(
            TaskState.READY, TaskEvent.CLAIM_GRANTED, actor=Actor.NODE
        )


def test_state_service_can_apply_task_event_returns_bool() -> None:
    """can_apply_task_event 不抛异常，返回布尔值。"""
    service = GitCoordinationStateService()
    assert service.can_apply_task_event(TaskState.PLANNED, TaskEvent.DEPENDENCIES_RESOLVED)
    assert not service.can_apply_task_event(TaskState.DONE, TaskEvent.PROGRESS_REPORTED)
    assert not service.can_apply_task_event(
        TaskState.READY, TaskEvent.CLAIM_GRANTED, actor=Actor.NODE
    )


def test_state_service_uses_injected_state_machine() -> None:
    """Service 接受外部注入的 TaskStateMachine 实例（便于测试与扩展）。"""
    sm = TaskStateMachine()
    service = GitCoordinationStateService(state_machine=sm)
    assert service._state_machine is sm
