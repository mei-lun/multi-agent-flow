"""TASK-024 单元测试：Assignment Epoch Fencing 防旧写。

验收标准覆盖（对应 TASK-024 文档与任务描述）：

1. **当前 epoch 通过**：``event_epoch == current_epoch`` 时校验通过；
2. **旧 epoch 拒绝**：``event_epoch < current_epoch`` 时被 fencing 拒绝
   （``AssignmentEpochStaleError``，``outcome=EPOCH_CHECK_STALE``）；
3. **未来 epoch 拒绝**：``event_epoch > current_epoch`` 时被拒绝
   （``AssignmentEpochError``，``outcome=EPOCH_CHECK_FUTURE``）；
4. **边界（epoch=1）**：首次分配 epoch=1 与 current_epoch=1 匹配通过；
5. **epoch 缺失拒绝**：``event_epoch=None`` 时被拒绝
   （``AssignmentEpochError``，``outcome=EPOCH_CHECK_MISSING``）；
6. **被拒绝事件记录判定**：``EpochCheckResult.decision`` 携带稳定判定值
   （``skipped_stale_epoch`` / ``skipped_future_epoch``），可供
   ``EventDecisionRepository.record_decision`` 直接使用；
7. **fencing 场景模拟**：节点 A 用 epoch=1 认领 → 超时重新分配 epoch=2 →
   节点 A 用 epoch=1 提交被拒绝 → 节点 B 用 epoch=2 提交通过；
8. **单调递增整数 epoch**：严格整数比较，不使用时间戳。

测试范围：
- ``packages/contracts_py/src/maf_contracts/coordination.py``：
  ``EpochCheckResult``、``EPOCH_CHECK_*`` / ``EPOCH_DECISION_*`` 常量。
- ``apps/server/src/maf_server/modules/git_coordination/service.py``：
  ``AssignmentEpochStaleError``、``AssignmentEpochError``、
  ``check_assignment_epoch``、``validate_assignment_epoch``。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# packages/artifact_schemas/src 不在 pyproject.toml 的 pythonpath 中（TASK-002 范围），
# 这里显式添加，使 maf_server.git_coordination.schemas 可导入。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT_SCHEMAS_SRC = _PROJECT_ROOT / "packages" / "artifact_schemas" / "src"
if str(_ARTIFACT_SCHEMAS_SRC) not in sys.path:
    sys.path.insert(0, str(_ARTIFACT_SCHEMAS_SRC))

from maf_contracts.coordination import (  # noqa: E402
    EPOCH_CHECK_FUTURE,
    EPOCH_CHECK_MISSING,
    EPOCH_CHECK_PASSED,
    EPOCH_CHECK_STALE,
    EPOCH_DECISION_SKIPPED_FUTURE,
    EPOCH_DECISION_SKIPPED_STALE,
    EpochCheckResult,
)
from maf_domain.errors import (  # noqa: E402
    ArgumentError,
    GitEventRejectedError,
    ReasonCode,
)
from maf_server.modules.git_coordination.service import (  # noqa: E402
    AssignmentEpochError,
    AssignmentEpochStaleError,
    check_assignment_epoch,
    validate_assignment_epoch,
)

# 同模块导入 CoordinationEventModel，供 validate_assignment_epoch 测试构造事件。
from maf_contracts.coordination import CoordinationEventModel  # noqa: E402


# --------------------------------------------------------------------------- #
# 固定常量与测试工厂
# --------------------------------------------------------------------------- #

_TASK_ID: str = "TASK-024-DEMO"
_NODE_ID_A: str = "node-aaaaaaaa-1111-1111-1111-111111111111"
_NODE_ID_B: str = "node-bbbbbbbb-2222-2222-2222-222222222222"
_CONTROL_COMMIT: str = "0123456789abcdef0123456789abcdef01234567"


def _make_event(
    *,
    event_id: str = "evt-0000000000000001",
    event_type: str = "PROGRESS_REPORTED",
    node_id: str = _NODE_ID_A,
    task_id: str | None = _TASK_ID,
    assignment_epoch: int | None = 1,
    based_on_control_commit: str = _CONTROL_COMMIT,
    occurred_at: str = "2026-07-17T00:00:00+00:00",
    payload: dict[str, Any] | None = None,
) -> CoordinationEventModel:
    """构造合法的 CoordinationEventModel（与 contracts_py 对齐）。

    ``assignment_epoch`` 默认为 1；传 ``None`` 表示事件未携带 epoch。
    """
    return CoordinationEventModel(
        schema_version=1,
        event_id=event_id,
        event_type=event_type,  # type: ignore[arg-type]
        node_id=node_id,
        task_id=task_id,
        assignment_id=f"asg-{assignment_epoch}" if assignment_epoch is not None else None,
        assignment_epoch=assignment_epoch,
        based_on_control_commit=based_on_control_commit,
        occurred_at=occurred_at,
        payload=payload if payload is not None else {},
    )


# --------------------------------------------------------------------------- #
# 验收 1：当前 epoch 通过
# --------------------------------------------------------------------------- #


class TestCurrentEpochPasses:
    """``event_epoch == current_epoch`` 时校验通过。"""

    def test_check_returns_passed_result(self) -> None:
        """check_assignment_epoch 返回 passed=True 的 EpochCheckResult。"""
        result = check_assignment_epoch(
            _TASK_ID, event_epoch=3, current_epoch=3
        )

        assert result.passed is True
        assert result.outcome == EPOCH_CHECK_PASSED
        assert result.reason_code is None
        assert result.decision is None
        assert result.task_id == _TASK_ID
        assert result.event_epoch == 3
        assert result.current_epoch == 3

    def test_validate_returns_none_on_pass(self) -> None:
        """validate_assignment_epoch 通过时不抛异常。"""
        event = _make_event(assignment_epoch=2)
        # 不应抛异常
        validate_assignment_epoch(event, current_epoch=2, task_id=_TASK_ID)

    def test_check_passes_with_large_epoch(self) -> None:
        """大 epoch 值（模拟多次重分配后）也能正确匹配。"""
        result = check_assignment_epoch(
            _TASK_ID, event_epoch=999, current_epoch=999
        )
        assert result.passed is True
        assert result.outcome == EPOCH_CHECK_PASSED


# --------------------------------------------------------------------------- #
# 验收 2：旧 epoch 拒绝（AssignmentEpochStaleError）
# --------------------------------------------------------------------------- #


class TestStaleEpochRejected:
    """``event_epoch < current_epoch`` 时被 fencing 拒绝。"""

    def test_check_returns_stale_result(self) -> None:
        """check_assignment_epoch 返回 passed=False, outcome=STALE。"""
        result = check_assignment_epoch(
            _TASK_ID, event_epoch=1, current_epoch=2
        )

        assert result.passed is False
        assert result.outcome == EPOCH_CHECK_STALE
        assert result.reason_code == ReasonCode.EVENT_EPOCH_STALE.value
        assert result.decision == EPOCH_DECISION_SKIPPED_STALE
        assert result.event_epoch == 1
        assert result.current_epoch == 2

    def test_validate_raises_stale_error(self) -> None:
        """validate_assignment_epoch 抛 AssignmentEpochStaleError。"""
        event = _make_event(assignment_epoch=1)
        with pytest.raises(AssignmentEpochStaleError) as exc_info:
            validate_assignment_epoch(event, current_epoch=2, task_id=_TASK_ID)

        err = exc_info.value
        assert err.reason_code == ReasonCode.EVENT_EPOCH_STALE.value
        assert err.retryable is False
        assert err.context["task_id"] == _TASK_ID
        assert err.context["event_epoch"] == 1
        assert err.context["current_epoch"] == 2
        assert err.context["event_id"] == event.event_id
        assert err.context["outcome"] == EPOCH_CHECK_STALE

    def test_stale_error_is_git_event_rejected(self) -> None:
        """AssignmentEpochStaleError 是 GitEventRejectedError 子类。"""
        event = _make_event(assignment_epoch=1)
        with pytest.raises(GitEventRejectedError):
            validate_assignment_epoch(event, current_epoch=3, task_id=_TASK_ID)

    def test_stale_by_large_margin(self) -> None:
        """旧 epoch 差距大（1 vs 100）也被正确拒绝。"""
        result = check_assignment_epoch(
            _TASK_ID, event_epoch=1, current_epoch=100
        )
        assert result.passed is False
        assert result.outcome == EPOCH_CHECK_STALE
        assert result.decision == EPOCH_DECISION_SKIPPED_STALE


# --------------------------------------------------------------------------- #
# 验收 3：未来 epoch 拒绝（AssignmentEpochError）
# --------------------------------------------------------------------------- #


class TestFutureEpochRejected:
    """``event_epoch > current_epoch`` 时被拒绝。"""

    def test_check_returns_future_result(self) -> None:
        """check_assignment_epoch 返回 passed=False, outcome=FUTURE。"""
        result = check_assignment_epoch(
            _TASK_ID, event_epoch=5, current_epoch=2
        )

        assert result.passed is False
        assert result.outcome == EPOCH_CHECK_FUTURE
        assert result.reason_code == ReasonCode.EVENT_SCHEMA_INVALID.value
        assert result.decision == EPOCH_DECISION_SKIPPED_FUTURE
        assert result.event_epoch == 5
        assert result.current_epoch == 2

    def test_validate_raises_epoch_error(self) -> None:
        """validate_assignment_epoch 抛 AssignmentEpochError。"""
        event = _make_event(assignment_epoch=3)
        with pytest.raises(AssignmentEpochError) as exc_info:
            validate_assignment_epoch(event, current_epoch=1, task_id=_TASK_ID)

        err = exc_info.value
        assert err.reason_code == ReasonCode.EVENT_SCHEMA_INVALID.value
        assert err.retryable is False
        assert err.context["task_id"] == _TASK_ID
        assert err.context["event_epoch"] == 3
        assert err.context["current_epoch"] == 1
        assert err.context["outcome"] == EPOCH_CHECK_FUTURE

    def test_future_error_is_git_event_rejected(self) -> None:
        """AssignmentEpochError 是 GitEventRejectedError 子类。"""
        event = _make_event(assignment_epoch=10)
        with pytest.raises(GitEventRejectedError):
            validate_assignment_epoch(event, current_epoch=1, task_id=_TASK_ID)


# --------------------------------------------------------------------------- #
# 验收 4：边界（epoch=1）
# --------------------------------------------------------------------------- #


class TestEpochOneBoundary:
    """epoch=1 是最小合法值（与 CoordinationEventModel.assignment_epoch ge=1 一致）。"""

    def test_epoch_one_matches_current_one(self) -> None:
        """首次分配 epoch=1 与 current_epoch=1 匹配通过。"""
        result = check_assignment_epoch(
            _TASK_ID, event_epoch=1, current_epoch=1
        )
        assert result.passed is True
        assert result.outcome == EPOCH_CHECK_PASSED

    def test_epoch_one_stale_against_current_two(self) -> None:
        """epoch=1 在 current_epoch=2 时被 fencing（典型重分配场景）。"""
        result = check_assignment_epoch(
            _TASK_ID, event_epoch=1, current_epoch=2
        )
        assert result.passed is False
        assert result.outcome == EPOCH_CHECK_STALE

    def test_validate_epoch_one_passes(self) -> None:
        """validate_assignment_epoch 在 epoch=1 时通过。"""
        event = _make_event(assignment_epoch=1)
        validate_assignment_epoch(event, current_epoch=1, task_id=_TASK_ID)


# --------------------------------------------------------------------------- #
# 验收 5：epoch 缺失拒绝
# --------------------------------------------------------------------------- #


class TestMissingEpochRejected:
    """``event_epoch=None`` 时被拒绝（事件未携带 epoch）。"""

    def test_check_returns_missing_result(self) -> None:
        """check_assignment_epoch 返回 passed=False, outcome=MISSING。"""
        result = check_assignment_epoch(
            _TASK_ID, event_epoch=None, current_epoch=1
        )

        assert result.passed is False
        assert result.outcome == EPOCH_CHECK_MISSING
        assert result.reason_code == ReasonCode.EVENT_SCHEMA_INVALID.value
        assert result.decision == EPOCH_DECISION_SKIPPED_FUTURE
        assert result.event_epoch is None
        assert result.current_epoch == 1

    def test_validate_raises_epoch_error_on_missing(self) -> None:
        """validate_assignment_epoch 在 epoch 缺失时抛 AssignmentEpochError。"""
        event = _make_event(assignment_epoch=None)
        with pytest.raises(AssignmentEpochError) as exc_info:
            validate_assignment_epoch(event, current_epoch=1, task_id=_TASK_ID)

        err = exc_info.value
        assert err.context["outcome"] == EPOCH_CHECK_MISSING
        assert err.context["event_epoch"] is None


# --------------------------------------------------------------------------- #
# 验收 6：被拒绝事件记录判定（decision 稳定值）
# --------------------------------------------------------------------------- #


class TestRejectedEventDecisionRecording:
    """被拒绝事件的 ``EpochCheckResult.decision`` 携带稳定判定值。

    TASK-024 验收"被拒绝的事件记录判定（通过 record_event_decision）"：
    ``decision`` 字段供 ``EventDecisionRepository.record_decision`` 作为
    ``decision`` 参数使用，``message`` 作为 ``error`` 参数。
    """

    def test_stale_decision_value_is_stable(self) -> None:
        """旧 epoch 拒绝的 decision='skipped_stale_epoch'。"""
        result = check_assignment_epoch(
            _TASK_ID, event_epoch=1, current_epoch=2
        )
        assert result.decision == "skipped_stale_epoch"
        assert result.decision == EPOCH_DECISION_SKIPPED_STALE

    def test_future_decision_value_is_stable(self) -> None:
        """未来 epoch 拒绝的 decision='skipped_future_epoch'。"""
        result = check_assignment_epoch(
            _TASK_ID, event_epoch=3, current_epoch=1
        )
        assert result.decision == "skipped_future_epoch"
        assert result.decision == EPOCH_DECISION_SKIPPED_FUTURE

    def test_passed_decision_is_none(self) -> None:
        """通过的 decision=None（无需记录 skipped 判定）。"""
        result = check_assignment_epoch(
            _TASK_ID, event_epoch=1, current_epoch=1
        )
        assert result.decision is None

    def test_decision_values_not_in_retryable_set(self) -> None:
        """skipped_stale_epoch / skipped_future_epoch 不在可重试判定集合中。

        这意味着 ``EventDecisionRepository.has_processed`` 对这些判定返回
        ``True``（旧/未来 epoch 事件不应重试）。
        """
        # 与 TASK-021 的 _RETRYABLE_DECISIONS = {"failed"} 对比：
        # skipped_stale_epoch 和 skipped_future_epoch 都不是 "failed"，
        # 因此 has_processed 返回 True（视为已处理，不可重试）。
        retryable_decisions = {"failed"}
        assert EPOCH_DECISION_SKIPPED_STALE not in retryable_decisions
        assert EPOCH_DECISION_SKIPPED_FUTURE not in retryable_decisions

    def test_rejected_result_message_is_non_empty(self) -> None:
        """被拒绝结果的 message 非空（供 record_decision 的 error 参数）。"""
        stale = check_assignment_epoch(
            _TASK_ID, event_epoch=1, current_epoch=2
        )
        assert stale.message
        assert "stale" in stale.message.lower()
        assert _TASK_ID in stale.message

        future = check_assignment_epoch(
            _TASK_ID, event_epoch=3, current_epoch=1
        )
        assert future.message
        assert "future" in future.message.lower()


# --------------------------------------------------------------------------- #
# 验收 7：fencing 场景模拟
# --------------------------------------------------------------------------- #


class TestFencingScenario:
    """模拟完整的 fencing 场景（协议 §7）。

    场景：
    1. 节点 A 用 epoch=1 认领任务，开始执行；
    2. 节点 A 超时，中央用 epoch=2 重新分配给节点 B；
    3. 节点 A 用 epoch=1 提交结果 → 被拒绝（stale epoch）；
    4. 节点 B 用 epoch=2 提交结果 → 通过。
    """

    def test_full_fencing_scenario(self) -> None:
        """完整 fencing 场景：旧 epoch 被拒，新 epoch 通过。"""
        # 1. 节点 A 认领，current_epoch=1
        current_epoch = 1
        event_a_progress = _make_event(
            event_id="evt-aaaa-progress-0001",
            node_id=_NODE_ID_A,
            assignment_epoch=1,
        )
        # 节点 A 的进度事件通过（epoch 匹配）
        validate_assignment_epoch(
            event_a_progress, current_epoch=current_epoch, task_id=_TASK_ID
        )

        # 2. 节点 A 超时，中央重新分配，current_epoch 递增到 2
        current_epoch = 2

        # 3. 节点 A 用 epoch=1 提交结果 → 被拒绝（stale）
        event_a_submission = _make_event(
            event_id="evt-aaaa-submission-0002",
            node_id=_NODE_ID_A,
            event_type="SUBMISSION_CREATED",
            assignment_epoch=1,  # 旧 epoch
        )
        with pytest.raises(AssignmentEpochStaleError) as exc_info:
            validate_assignment_epoch(
                event_a_submission,
                current_epoch=current_epoch,
                task_id=_TASK_ID,
            )
        assert exc_info.value.context["event_epoch"] == 1
        assert exc_info.value.context["current_epoch"] == 2

        # 4. 节点 B 用 epoch=2 提交结果 → 通过
        event_b_submission = _make_event(
            event_id="evt-bbbb-submission-0003",
            node_id=_NODE_ID_B,
            event_type="SUBMISSION_CREATED",
            assignment_epoch=2,  # 当前 epoch
        )
        validate_assignment_epoch(
            event_b_submission,
            current_epoch=current_epoch,
            task_id=_TASK_ID,
        )

    def test_fencing_check_version_of_scenario(self) -> None:
        """用 check_assignment_epoch（非抛异常版本）验证同一场景。"""
        current_epoch = 1
        # 节点 A epoch=1 通过
        assert check_assignment_epoch(
            _TASK_ID, event_epoch=1, current_epoch=current_epoch
        ).passed

        # 重分配
        current_epoch = 2
        # 节点 A epoch=1 被拒
        stale_result = check_assignment_epoch(
            _TASK_ID, event_epoch=1, current_epoch=current_epoch
        )
        assert not stale_result.passed
        assert stale_result.decision == EPOCH_DECISION_SKIPPED_STALE

        # 节点 B epoch=2 通过
        assert check_assignment_epoch(
            _TASK_ID, event_epoch=2, current_epoch=current_epoch
        ).passed

    def test_progress_event_from_stale_node_rejected(self) -> None:
        """PROGRESS_REPORTED 事件也受 fencing 约束。"""
        current_epoch = 3
        event = _make_event(
            event_type="PROGRESS_REPORTED",
            assignment_epoch=2,  # 旧 epoch
        )
        with pytest.raises(AssignmentEpochStaleError):
            validate_assignment_epoch(
                event, current_epoch=current_epoch, task_id=_TASK_ID
            )

    def test_blocked_event_from_stale_node_rejected(self) -> None:
        """BLOCKED_REPORTED 事件也受 fencing 约束。"""
        current_epoch = 3
        event = _make_event(
            event_type="BLOCKED_REPORTED",
            assignment_epoch=2,  # 旧 epoch
        )
        with pytest.raises(AssignmentEpochStaleError):
            validate_assignment_epoch(
                event, current_epoch=current_epoch, task_id=_TASK_ID
            )


# --------------------------------------------------------------------------- #
# 验收 8：单调递增整数 epoch（不用时间戳）
# --------------------------------------------------------------------------- #


class TestMonotonicIntegerEpoch:
    """epoch 是单调递增整数，严格算术比较，不使用时间戳。"""

    def test_epoch_comparison_is_arithmetic(self) -> None:
        """epoch 比较是整数算术比较，不是字符串或时间戳比较。"""
        # 1 < 2 < 10 < 100（算术序，不是字典序）
        assert check_assignment_epoch(
            _TASK_ID, event_epoch=1, current_epoch=2
        ).outcome == EPOCH_CHECK_STALE
        assert check_assignment_epoch(
            _TASK_ID, event_epoch=10, current_epoch=2
        ).outcome == EPOCH_CHECK_FUTURE
        assert check_assignment_epoch(
            _TASK_ID, event_epoch=100, current_epoch=100
        ).outcome == EPOCH_CHECK_PASSED

    def test_epoch_type_is_int(self) -> None:
        """EpochCheckResult 的 epoch 字段是 int 类型（非 float/str）。"""
        result = check_assignment_epoch(
            _TASK_ID, event_epoch=5, current_epoch=5
        )
        assert isinstance(result.event_epoch, int)
        assert isinstance(result.current_epoch, int)

    def test_consecutive_reassignment_epochs(self) -> None:
        """模拟连续重分配：epoch 1→2→3→4，每次旧 epoch 都被拒。"""
        for current in range(1, 5):
            # 当前 epoch 通过
            assert check_assignment_epoch(
                _TASK_ID, event_epoch=current, current_epoch=current
            ).passed
            # 旧 epoch 被拒
            if current > 1:
                stale = check_assignment_epoch(
                    _TASK_ID, event_epoch=current - 1, current_epoch=current
                )
                assert not stale.passed
                assert stale.outcome == EPOCH_CHECK_STALE
            # 未来 epoch 被拒
            future = check_assignment_epoch(
                _TASK_ID, event_epoch=current + 1, current_epoch=current
            )
            assert not future.passed
            assert future.outcome == EPOCH_CHECK_FUTURE


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #


class TestArgumentValidation:
    """``check_assignment_epoch`` / ``validate_assignment_epoch`` 参数校验。"""

    def test_empty_task_id_raises_argument_error(self) -> None:
        """task_id 为空抛 ArgumentError。"""
        with pytest.raises(ArgumentError):
            check_assignment_epoch("", event_epoch=1, current_epoch=1)

    def test_current_epoch_zero_raises_argument_error(self) -> None:
        """current_epoch=0 抛 ArgumentError（必须 >= 1）。"""
        with pytest.raises(ArgumentError):
            check_assignment_epoch(_TASK_ID, event_epoch=1, current_epoch=0)

    def test_current_epoch_negative_raises_argument_error(self) -> None:
        """current_epoch 负数抛 ArgumentError。"""
        with pytest.raises(ArgumentError):
            check_assignment_epoch(_TASK_ID, event_epoch=1, current_epoch=-1)

    def test_validate_empty_task_id_raises_argument_error(self) -> None:
        """validate_assignment_epoch 在 task_id 为空时抛 ArgumentError。"""
        event = _make_event(assignment_epoch=1)
        with pytest.raises(ArgumentError):
            validate_assignment_epoch(event, current_epoch=1, task_id="")


# --------------------------------------------------------------------------- #
# EpochCheckResult 不可变性
# --------------------------------------------------------------------------- #


class TestEpochCheckResultImmutable:
    """EpochCheckResult 是 frozen dataclass，字段不可修改。"""

    def test_result_is_frozen(self) -> None:
        """frozen=True 防止调用方误改结果。"""
        result = check_assignment_epoch(
            _TASK_ID, event_epoch=1, current_epoch=1
        )
        with pytest.raises(Exception):
            result.passed = False  # type: ignore[misc]

    def test_result_is_hashable(self) -> None:
        """frozen dataclass 可哈希（便于在集合/字典中使用）。"""
        result1 = check_assignment_epoch(
            _TASK_ID, event_epoch=1, current_epoch=1
        )
        result2 = check_assignment_epoch(
            _TASK_ID, event_epoch=1, current_epoch=1
        )
        # 相同输入产生相同结果，可哈希且相等
        assert result1 == result2
        assert hash(result1) == hash(result2)
