"""TASK-023 单元测试：Claim 选择与分配（TaskAllocator.choose_claim）。

验收标准覆盖（对应 TASK-023 文档与任务描述）：

1. **确定性**：相同输入产生相同输出（``task_id`` 与 ``assignment_epoch`` 都
   由确定排序与实例状态决定，不使用随机数/时间戳/LLM）；
2. **能力匹配**：节点不满足 ``requirements.required_capabilities`` 时不分配；
3. **优先级排序**：高优先级（``priority`` 数值大）先分配；
4. **字典序 tiebreaker**：同优先级按 ``task_id`` 字典序升序选择；
5. **排除已处理**：节点已是 ``assignment.node_id`` 的任务被排除；
6. **无可用任务**：无匹配任务时返回 ``task_id=None``、
   ``reason="no_matching_tasks"``、``assignment_epoch=None``；
7. **assignment_epoch 递增**：每次成功分配 epoch 递增，无匹配时不递增；
8. **多任务场景**：snapshot 含多任务时按规则选最优。

测试范围：
- ``apps/server/src/maf_server/modules/git_coordination/service.py``：
  ``TaskAllocator``、``ClaimDecision``。
"""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

# packages/artifact_schemas/src 不在 pyproject.toml 的 pythonpath 中（TASK-002 范围），
# 这里显式添加，使 maf_server.git_coordination.schemas 可导入。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT_SCHEMAS_SRC = _PROJECT_ROOT / "packages" / "artifact_schemas" / "src"
if str(_ARTIFACT_SCHEMAS_SRC) not in sys.path:
    sys.path.insert(0, str(_ARTIFACT_SCHEMAS_SRC))

from maf_domain.errors import ArgumentError  # noqa: E402
from maf_server.modules.git_coordination.service import (  # noqa: E402
    ClaimDecision,
    CoordinationSnapshot,
    TaskAllocator,
)

# --------------------------------------------------------------------------- #
# 固定常量与测试工厂
# --------------------------------------------------------------------------- #

_NODE_ID_A: str = "node-aaaaaaaa-1111-1111-1111-111111111111"
_NODE_ID_B: str = "node-bbbbbbbb-2222-2222-2222-222222222222"
_CONTROL_COMMIT: str = "0123456789abcdef0123456789abcdef01234567"


def _make_manifest(
    *,
    node_id: str = _NODE_ID_A,
    capabilities: list[str] | None = None,
    capacity: int = 4,
    status: str = "ACTIVE",
) -> dict[str, Any]:
    """构造合法的 NodeManifest dict（与 contracts_py.NodeManifest 对齐）。"""
    return {
        "schema_version": 1,
        "node_id": node_id,
        "display_name": f"Test Node {node_id[:9]}",
        "git_identity": {
            "name": "Test Bot",
            "email": "bot@example.test",
        },
        "capabilities": capabilities if capabilities is not None else ["python", "docker"],
        "model_aliases": [],
        "docker_profiles": ["generic"],
        "capacity": capacity,
        "status": status,
        "software_version": "0.1.0",
        "version": 1,
    }


def _make_task(
    *,
    task_id: str = "TASK-001",
    status: str = "READY",
    priority: int = 5,
    required_capabilities: list[str] | None = None,
    assignment: dict[str, Any] | None = None,
    dependencies: list[str] | None = None,
) -> dict[str, Any]:
    """构造合法的 CoordinationTask dict（与 contracts_py.CoordinationTask 对齐）。

    ``requirements`` 内嵌 ``required_capabilities`` 列表，遵循
    《多 Agent 协同工具需求分析文档》§12.6 的 ``required_capabilities`` 字段名。
    """
    requirements: dict[str, Any] = {}
    if required_capabilities is not None:
        requirements["required_capabilities"] = list(required_capabilities)
    return {
        "schema_version": 1,
        "task_id": task_id,
        "parent_task_id": None,
        "title": f"Task {task_id}",
        "description": f"Test task {task_id}",
        "status": status,
        "priority": priority,
        "requirements": requirements,
        "dependencies": dependencies if dependencies is not None else [],
        "assignment": assignment,
        "progress": {
            "percent": 0,
            "completed_items": [],
            "remaining_items": [],
            "problems": [],
            "current_head_commit": None,
            "test_summary": None,
            "last_reported_at": None,
        },
        "delivery": {
            "branch": None,
            "base_commit": None,
            "head_commit": None,
            "pull_request_url": None,
            "changed_paths": [],
            "test_report_path": None,
            "known_issues": [],
        },
        "version": 1,
    }


def _make_snapshot(
    *,
    tasks: list[dict[str, Any]] | None = None,
    project_id: str = "proj-test-001",
) -> CoordinationSnapshot:
    """构造合法的 CoordinationSnapshot dict（与 service.py 对齐）。"""
    return CoordinationSnapshot(
        project_id=project_id,
        control_commit=_CONTROL_COMMIT,
        commit_timestamp="2026-07-17T00:00:00+00:00",
        project_yaml={
            "schema_version": 1,
            "project_id": project_id,
            "control_branch": "maf/control",
            "coordination_mode": "git_single_writer",
        },
        status_md="# Status\n",
        tasks_paths=[],
        nodes_paths=[],
        events_paths=[],
        tasks=list(tasks) if tasks is not None else [],
        nodes=[],
        generated_at="2026-07-17T00:00:00+00:00",
    )


# --------------------------------------------------------------------------- #
# 验收 1：确定性（相同输入相同输出）
# --------------------------------------------------------------------------- #


class TestDeterminism:
    """``choose_claim`` 是确定性的：相同输入产生相同输出。"""

    def test_two_allocators_same_input_same_output(self) -> None:
        """两个独立 allocator（相同 initial_epoch）对同一 snapshot 返回相同结果。"""
        task = _make_task(task_id="TASK-001", priority=5, required_capabilities=["python"])
        snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=["python", "docker"])

        allocator_a = TaskAllocator()
        allocator_b = TaskAllocator()

        decision_a = allocator_a.choose_claim(snapshot, _NODE_ID_A, manifest)
        decision_b = allocator_b.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision_a == decision_b
        assert decision_a["task_id"] == "TASK-001"
        assert decision_a["assignment_epoch"] == 1

    def test_same_allocator_called_twace_with_same_snapshot_returns_same_task_id(
        self,
    ) -> None:
        """同一 allocator 对同一 snapshot 调用两次，task_id 相同（epoch 递增是设计）。

        注意：``assignment_epoch`` 必须递增（每次成功分配 +1，用于 fencing），
        但 ``task_id`` 选择是确定性的（相同 snapshot 选同一个 task）。
        """
        task = _make_task(task_id="TASK-001", priority=5, required_capabilities=["python"])
        snapshot = _make_snapshot(tasks=[task])
        # 第二个 snapshot 内容相同（独立 dict，避免引用别名干扰）。
        snapshot_2 = _make_snapshot(
            tasks=[_make_task(task_id="TASK-001", priority=5, required_capabilities=["python"])]
        )
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        first = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)
        second = allocator.choose_claim(snapshot_2, _NODE_ID_A, manifest)

        # task_id 选择确定性：两次都选 TASK-001（snapshot 内容相同）。
        assert first["task_id"] == "TASK-001"
        assert second["task_id"] == "TASK-001"
        # epoch 单调递增（验收 7）。
        assert second["assignment_epoch"] == first["assignment_epoch"] + 1

    def test_selection_independent_of_task_list_order(self) -> None:
        """候选任务的输入顺序不影响选择结果（确定性排序）。"""
        task_low = _make_task(task_id="TASK-AAA", priority=3, required_capabilities=[])
        task_high = _make_task(task_id="TASK-BBB", priority=9, required_capabilities=[])

        snapshot_high_first = _make_snapshot(tasks=[task_high, task_low])
        snapshot_low_first = _make_snapshot(tasks=[task_low, task_high])

        manifest = _make_manifest(capabilities=["python"])
        allocator_a = TaskAllocator()
        allocator_b = TaskAllocator()

        decision_a = allocator_a.choose_claim(snapshot_high_first, _NODE_ID_A, manifest)
        decision_b = allocator_b.choose_claim(snapshot_low_first, _NODE_ID_A, manifest)

        # 无论输入顺序，高优先级任务 TASK-BBB 都被选中。
        assert decision_a["task_id"] == "TASK-BBB"
        assert decision_b["task_id"] == "TASK-BBB"
        assert decision_a == decision_b


# --------------------------------------------------------------------------- #
# 验收 2：能力匹配（节点不满足任务要求时不分配）
# --------------------------------------------------------------------------- #


class TestCapabilityMatching:
    """``choose_claim`` 严格按能力子集匹配。"""

    def test_node_without_required_capability_gets_no_task(self) -> None:
        """节点缺少任务要求的某项能力时不分配。"""
        task = _make_task(
            task_id="TASK-001",
            required_capabilities=["python", "docker", "gpu"],
        )
        snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=["python", "docker"])  # 缺 gpu

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] is None
        assert decision["reason"] == "no_matching_tasks"
        assert decision["assignment_epoch"] is None

    def test_node_with_exact_required_capabilities_gets_task(self) -> None:
        """节点恰好满足任务要求的能力时分配（能力集合相等也算子集）。"""
        task = _make_task(
            task_id="TASK-001",
            required_capabilities=["python", "docker"],
        )
        snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=["python", "docker"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] == "TASK-001"
        assert decision["reason"] == "claim_granted"

    def test_node_with_superset_capabilities_gets_task(self) -> None:
        """节点能力是任务要求能力的超集时分配。"""
        task = _make_task(
            task_id="TASK-001",
            required_capabilities=["python"],
        )
        snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=["python", "docker", "gpu"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] == "TASK-001"

    def test_task_without_required_capabilities_matches_any_node(self) -> None:
        """任务未声明 ``required_capabilities`` 时匹配任意节点（空集是任意集合子集）。"""
        task = _make_task(task_id="TASK-001", required_capabilities=None)
        snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=[])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] == "TASK-001"
        assert decision["reason"] == "claim_granted"

    def test_node_capability_mismatch_skips_only_ineligible_tasks(self) -> None:
        """节点能力只匹配部分任务时，跳过不匹配的、分配匹配的。"""
        task_ineligible = _make_task(
            task_id="TASK-001",
            priority=10,
            required_capabilities=["gpu"],
        )
        task_eligible = _make_task(
            task_id="TASK-002",
            priority=1,
            required_capabilities=["python"],
        )
        snapshot = _make_snapshot(tasks=[task_ineligible, task_eligible])
        manifest = _make_manifest(capabilities=["python"])  # 无 gpu

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        # 高优先级 TASK-001 缺能力被跳过；TASK-002 被分配。
        assert decision["task_id"] == "TASK-002"

    def test_empty_required_capabilities_list_matches_any_node(self) -> None:
        """任务 ``required_capabilities=[]`` 与 ``None`` 等价，匹配任意节点。"""
        task = _make_task(task_id="TASK-001", required_capabilities=[])
        snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=[])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] == "TASK-001"


# --------------------------------------------------------------------------- #
# 验收 3：优先级排序（高优先级先分配）
# --------------------------------------------------------------------------- #


class TestPriorityOrdering:
    """``choose_claim`` 按 ``priority`` 降序选择。"""

    def test_higher_priority_task_selected(self) -> None:
        """两个候选能力都匹配时，priority 数值大的优先。"""
        task_low = _make_task(task_id="TASK-001", priority=1, required_capabilities=[])
        task_high = _make_task(task_id="TASK-002", priority=10, required_capabilities=[])
        snapshot = _make_snapshot(tasks=[task_low, task_high])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] == "TASK-002"
        assert decision["reason"] == "claim_granted"

    def test_priority_overrides_task_id_order(self) -> None:
        """高优先级任务即使 task_id 字典序靠后也被选中。"""
        # TASK-AAA 优先级低，TASK-ZZZ 优先级高：应选 TASK-ZZZ。
        task_aaa_low = _make_task(task_id="TASK-AAA", priority=1, required_capabilities=[])
        task_zzz_high = _make_task(task_id="TASK-ZZZ", priority=9, required_capabilities=[])
        snapshot = _make_snapshot(tasks=[task_aaa_low, task_zzz_high])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] == "TASK-ZZZ"

    def test_equal_priority_falls_back_to_task_id(self) -> None:
        """同优先级时按 task_id 字典序升序（验收 4 的前置）。"""
        task_b = _make_task(task_id="TASK-BBB", priority=5, required_capabilities=[])
        task_a = _make_task(task_id="TASK-AAA", priority=5, required_capabilities=[])
        snapshot = _make_snapshot(tasks=[task_b, task_a])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] == "TASK-AAA"

    def test_negative_priority_supported(self) -> None:
        """负优先级也被支持（数值大的优先）。"""
        task_neg = _make_task(task_id="TASK-001", priority=-5, required_capabilities=[])
        task_zero = _make_task(task_id="TASK-002", priority=0, required_capabilities=[])
        snapshot = _make_snapshot(tasks=[task_neg, task_zero])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        # 0 > -5，选 TASK-002。
        assert decision["task_id"] == "TASK-002"


# --------------------------------------------------------------------------- #
# 验收 4：同优先级按 task_id 字典序 tiebreaker
# --------------------------------------------------------------------------- #


class TestTaskIdLexicographicTiebreaker:
    """``choose_claim`` 同优先级按 ``task_id`` 字典序升序。"""

    def test_lexicographic_order_ascending(self) -> None:
        """同优先级时 task_id 字典序小的优先。"""
        tasks = [
            _make_task(task_id=f"TASK-{suffix:03d}", priority=5, required_capabilities=[])
            for suffix in range(20, 0, -1)
        ]
        snapshot = _make_snapshot(tasks=tasks)
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] == "TASK-001"

    def test_lexicographic_case_sensitive(self) -> None:
        """字典序按 Python 默认字符串比较（大写字母 < 小写字母）。

        TASK-AAA < TASK-aaa（'A' < 'a'），选 TASK-AAA。
        """
        task_upper = _make_task(task_id="TASK-AAA", priority=5, required_capabilities=[])
        task_lower = _make_task(task_id="TASK-aaa", priority=5, required_capabilities=[])
        snapshot = _make_snapshot(tasks=[task_lower, task_upper])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] == "TASK-AAA"

    def test_lexicographic_with_different_prefixes(self) -> None:
        """不同前缀的 task_id 也按字典序选择。"""
        task_x = _make_task(task_id="X-001", priority=5, required_capabilities=[])
        task_a = _make_task(task_id="A-001", priority=5, required_capabilities=[])
        task_m = _make_task(task_id="M-001", priority=5, required_capabilities=[])
        snapshot = _make_snapshot(tasks=[task_x, task_a, task_m])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] == "A-001"


# --------------------------------------------------------------------------- #
# 验收 5：排除节点已处理任务
# --------------------------------------------------------------------------- #


class TestExcludeNodeActiveTasks:
    """``choose_claim`` 排除节点已在处理的任务（assignment.node_id == node_id）。"""

    def test_task_with_stale_assignment_to_same_node_is_excluded(self) -> None:
        """READY 任务残留 assignment.node_id == node_id 时被排除。

        场景：lease 过期后 task 被 REQUEUED 回 READY，但 assignment 字段未清。
        节点不应再次认领同一任务。
        """
        stale_assignment = {
            "node_id": _NODE_ID_A,
            "assignment_id": "asg-stale-001",
            "assignment_epoch": 1,
            "assigned_at": "2026-07-17T00:00:00+00:00",
            "expires_at": "2026-07-17T01:00:00+00:00",
            "based_on_control_commit": _CONTROL_COMMIT,
        }
        # TASK-001 残留 assignment 指向 node_id_A，但状态是 READY。
        task_stale = _make_task(
            task_id="TASK-001",
            status="READY",
            priority=10,
            required_capabilities=[],
            assignment=stale_assignment,
        )
        snapshot = _make_snapshot(tasks=[task_stale])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        # assignment is not None → 视为未真正 READY，跳过。
        assert decision["task_id"] is None
        assert decision["reason"] == "no_matching_tasks"

    def test_node_with_active_assigned_task_can_get_another(self) -> None:
        """节点已在处理 ASSIGNED 任务时，仍可被分配其他 READY 任务。

        本测试验证：snapshot 含一个 ASSIGNED（节点 A 拥有）任务 + 一个 READY 任务，
        节点 A 的 choose_claim 应选 READY 任务（排除 ASSIGNED 任务因其状态非 READY）。
        """
        active_assignment = {
            "node_id": _NODE_ID_A,
            "assignment_id": "asg-active-001",
            "assignment_epoch": 1,
            "assigned_at": "2026-07-17T00:00:00+00:00",
            "expires_at": "2026-07-17T01:00:00+00:00",
            "based_on_control_commit": _CONTROL_COMMIT,
        }
        task_active = _make_task(
            task_id="TASK-ACTIVE",
            status="ASSIGNED",
            priority=10,
            required_capabilities=[],
            assignment=active_assignment,
        )
        task_ready = _make_task(
            task_id="TASK-READY",
            status="READY",
            priority=1,
            required_capabilities=[],
        )
        snapshot = _make_snapshot(tasks=[task_active, task_ready])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        # ASSIGNED 任务被排除（状态非 READY）；READY 任务被分配。
        assert decision["task_id"] == "TASK-READY"

    def test_task_assigned_to_other_node_not_excluded_for_this_node(self) -> None:
        """assignment.node_id 指向其他节点的 READY 任务对当前节点不排除。

        注意：READY + assignment != None 的任务本身会被 step 2 跳过
        （视为未真正 READY），所以本测试用 ASSIGNED 状态验证排除只针对同节点。
        """
        other_assignment = {
            "node_id": _NODE_ID_B,
            "assignment_id": "asg-other-001",
            "assignment_epoch": 1,
            "assigned_at": "2026-07-17T00:00:00+00:00",
            "expires_at": "2026-07-17T01:00:00+00:00",
            "based_on_control_commit": _CONTROL_COMMIT,
        }
        task_assigned_to_b = _make_task(
            task_id="TASK-001",
            status="ASSIGNED",  # ASSIGNED 状态、分配给 B
            priority=10,
            required_capabilities=[],
            assignment=other_assignment,
        )
        task_ready = _make_task(
            task_id="TASK-002",
            status="READY",
            priority=1,
            required_capabilities=[],
        )
        snapshot = _make_snapshot(tasks=[task_assigned_to_b, task_ready])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        # 节点 A 调用：TASK-001 是 ASSIGNED（非 READY）被排除；
        # TASK-001 分配给 B，不影响 A 的候选。
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] == "TASK-002"


# --------------------------------------------------------------------------- #
# 验收 6：无可用任务返回 task_id=None
# --------------------------------------------------------------------------- #


class TestNoMatchingTasks:
    """``choose_claim`` 无可用任务时返回 ``task_id=None``。"""

    def test_empty_snapshot_returns_no_matching_tasks(self) -> None:
        """空 snapshot（无任务）返回 no_matching_tasks。"""
        snapshot = _make_snapshot(tasks=[])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] is None
        assert decision["reason"] == "no_matching_tasks"
        assert decision["assignment_epoch"] is None
        assert decision["node_id"] == _NODE_ID_A

    def test_no_ready_tasks_returns_no_matching_tasks(self) -> None:
        """snapshot 只含非 READY 任务时返回 no_matching_tasks。"""
        task_planned = _make_task(task_id="TASK-001", status="PLANNED")
        task_assigned = _make_task(task_id="TASK-002", status="ASSIGNED")
        task_done = _make_task(task_id="TASK-003", status="DONE")
        snapshot = _make_snapshot(tasks=[task_planned, task_assigned, task_done])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] is None
        assert decision["reason"] == "no_matching_tasks"

    def test_all_ready_tasks_require_unmatched_capabilities(self) -> None:
        """所有 READY 任务都要求节点没有的能力时返回 no_matching_tasks。"""
        task = _make_task(
            task_id="TASK-001",
            status="READY",
            required_capabilities=["gpu"],
        )
        snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=["python"])  # 无 gpu

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] is None
        assert decision["reason"] == "no_matching_tasks"

    def test_no_matching_tasks_does_not_increment_epoch(self) -> None:
        """无匹配任务时 ``assignment_epoch`` 不递增（验收 7 前置）。"""
        snapshot = _make_snapshot(tasks=[])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        epoch_before = allocator.next_assignment_epoch
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)
        epoch_after = allocator.next_assignment_epoch

        assert decision["task_id"] is None
        assert epoch_before == epoch_after == 1  # 默认 initial_epoch=0 → next=1

    def test_no_matching_tasks_decision_structure(self) -> None:
        """无匹配任务的 ClaimDecision 字段完整。"""
        snapshot = _make_snapshot(tasks=[])
        manifest = _make_manifest()

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert isinstance(decision, dict)
        assert set(decision.keys()) == {
            "task_id",
            "node_id",
            "reason",
            "assignment_epoch",
        }
        assert decision["task_id"] is None
        assert decision["node_id"] == _NODE_ID_A
        assert decision["reason"] == "no_matching_tasks"
        assert decision["assignment_epoch"] is None


# --------------------------------------------------------------------------- #
# 验收 7：assignment_epoch 递增
# --------------------------------------------------------------------------- #


class TestAssignmentEpochIncrement:
    """``assignment_epoch`` 单调递增（用于 TASK-024 fencing）。"""

    def test_first_assignment_returns_epoch_one(self) -> None:
        """默认 initial_epoch=0，首次分配返回 epoch=1（1-based）。"""
        task = _make_task(task_id="TASK-001", required_capabilities=[])
        snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["assignment_epoch"] == 1

    def test_epoch_increments_on_each_successful_assignment(self) -> None:
        """每次成功分配 epoch 递增 1。"""
        task = _make_task(task_id="TASK-001", required_capabilities=[])
        snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        d1 = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)
        d2 = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)
        d3 = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert d1["assignment_epoch"] == 1
        assert d2["assignment_epoch"] == 2
        assert d3["assignment_epoch"] == 3

    def test_epoch_monotonically_increasing(self) -> None:
        """epoch 序列严格单调递增（每个 > 前一个）。"""
        task = _make_task(task_id="TASK-001", required_capabilities=[])
        snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        epochs = [
            allocator.choose_claim(snapshot, _NODE_ID_A, manifest)["assignment_epoch"]
            for _ in range(5)
        ]
        for i in range(1, len(epochs)):
            assert epochs[i] > epochs[i - 1]
        assert epochs == [1, 2, 3, 4, 5]

    def test_epoch_does_not_increment_on_no_match(self) -> None:
        """无匹配任务时 epoch 不递增。"""
        empty_snapshot = _make_snapshot(tasks=[])
        task = _make_task(task_id="TASK-001", required_capabilities=[])
        task_snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        # 无匹配 → epoch 不变。
        d_no = allocator.choose_claim(empty_snapshot, _NODE_ID_A, manifest)
        assert d_no["assignment_epoch"] is None
        assert allocator.next_assignment_epoch == 1

        # 有匹配 → epoch=1。
        d_yes = allocator.choose_claim(task_snapshot, _NODE_ID_A, manifest)
        assert d_yes["assignment_epoch"] == 1
        assert allocator.next_assignment_epoch == 2

        # 再次无匹配 → epoch 不变（仍为 2）。
        d_no2 = allocator.choose_claim(empty_snapshot, _NODE_ID_A, manifest)
        assert d_no2["assignment_epoch"] is None
        assert allocator.next_assignment_epoch == 2

    def test_custom_initial_epoch_respected(self) -> None:
        """``initial_epoch`` 参数决定起始值，首次分配返回 ``initial_epoch + 1``。"""
        task = _make_task(task_id="TASK-001", required_capabilities=[])
        snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator(initial_epoch=99)
        assert allocator.next_assignment_epoch == 100
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)
        assert decision["assignment_epoch"] == 100

    def test_negative_initial_epoch_raises(self) -> None:
        """``initial_epoch`` 为负数抛 ArgumentError。"""
        with pytest.raises(ArgumentError, match="initial_epoch"):
            TaskAllocator(initial_epoch=-1)

    def test_epoch_shared_across_nodes(self) -> None:
        """epoch 是 allocator 实例全局状态，跨节点也单调递增（用于 fencing）。

        场景：节点 A 与节点 B 共享同一 allocator（中央调度器），epoch 全局递增。
        """
        task = _make_task(task_id="TASK-001", required_capabilities=[])
        snapshot = _make_snapshot(tasks=[task])
        manifest_a = _make_manifest(node_id=_NODE_ID_A, capabilities=["python"])
        manifest_b = _make_manifest(node_id=_NODE_ID_B, capabilities=["python"])

        allocator = TaskAllocator()
        d_a = allocator.choose_claim(snapshot, _NODE_ID_A, manifest_a)
        d_b = allocator.choose_claim(snapshot, _NODE_ID_B, manifest_b)

        assert d_a["assignment_epoch"] == 1
        assert d_b["assignment_epoch"] == 2  # 全局递增，跨节点也单调

    def test_next_assignment_epoch_property(self) -> None:
        """``next_assignment_epoch`` 属性反映下一次分配的 epoch 值。"""
        task = _make_task(task_id="TASK-001", required_capabilities=[])
        snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        assert allocator.next_assignment_epoch == 1

        allocator.choose_claim(snapshot, _NODE_ID_A, manifest)
        assert allocator.next_assignment_epoch == 2

        allocator.choose_claim(snapshot, _NODE_ID_A, manifest)
        assert allocator.next_assignment_epoch == 3


# --------------------------------------------------------------------------- #
# 验收 8：多任务场景
# --------------------------------------------------------------------------- #


class TestMultiTaskScenario:
    """``choose_claim`` 在多任务场景下按规则选最优。"""

    def test_selects_highest_priority_among_many(self) -> None:
        """10 个候选中选 priority 最高的。"""
        tasks = [
            _make_task(
                task_id=f"TASK-{i:03d}",
                priority=i,
                required_capabilities=["python"],
            )
            for i in range(1, 11)
        ]
        snapshot = _make_snapshot(tasks=tasks)
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        # priority=10 的 TASK-010 最高。
        assert decision["task_id"] == "TASK-010"

    def test_selects_lowest_task_id_among_same_priority(self) -> None:
        """同优先级的多任务中选 task_id 字典序最小的。"""
        tasks = [
            _make_task(
                task_id=f"TASK-{suffix}",
                priority=5,
                required_capabilities=["python"],
            )
            for suffix in ["ZETA", "ALPHA", "MIDDLE", "BETA"]
        ]
        snapshot = _make_snapshot(tasks=tasks)
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] == "TASK-ALPHA"

    def test_mixed_capabilities_priorities_and_statuses(self) -> None:
        """混合场景：综合能力、优先级、状态、字典序选最优。

        - TASK-A：READY, priority=10, requires=['gpu']（节点无 gpu → 不匹配）
        - TASK-B：READY, priority=5, requires=['python']（匹配）
        - TASK-C：ASSIGNED, priority=10, requires=[]（非 READY → 排除）
        - TASK-D：READY, priority=8, requires=['docker']（匹配）
        - TASK-E：PLANNED, priority=10, requires=[]（非 READY → 排除）

        预期：候选 = {B (p=5), D (p=8)}，选 D（priority=8 > 5）。
        """
        tasks = [
            _make_task(task_id="TASK-A", status="READY", priority=10, required_capabilities=["gpu"]),
            _make_task(task_id="TASK-B", status="READY", priority=5, required_capabilities=["python"]),
            _make_task(task_id="TASK-C", status="ASSIGNED", priority=10, required_capabilities=[]),
            _make_task(task_id="TASK-D", status="READY", priority=8, required_capabilities=["docker"]),
            _make_task(task_id="TASK-E", status="PLANNED", priority=10, required_capabilities=[]),
        ]
        snapshot = _make_snapshot(tasks=tasks)
        manifest = _make_manifest(capabilities=["python", "docker"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] == "TASK-D"
        assert decision["reason"] == "claim_granted"

    def test_tie_priority_and_capability_tiebreaker(self) -> None:
        """同优先级 + 都匹配能力 → 按 task_id 字典序。"""
        tasks = [
            _make_task(task_id="TASK-003", priority=5, required_capabilities=["python"]),
            _make_task(task_id="TASK-001", priority=5, required_capabilities=["python"]),
            _make_task(task_id="TASK-002", priority=5, required_capabilities=["python"]),
        ]
        snapshot = _make_snapshot(tasks=tasks)
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] == "TASK-001"

    def test_does_not_mutate_snapshot(self) -> None:
        """``choose_claim`` 不修改 snapshot（只读语义）。"""
        task = _make_task(task_id="TASK-001", priority=5, required_capabilities=["python"])
        snapshot = _make_snapshot(tasks=[task])
        snapshot_before = deepcopy(snapshot)
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert snapshot == snapshot_before

    def test_does_not_mutate_node_manifest(self) -> None:
        """``choose_claim`` 不修改 NodeManifest（只读语义）。"""
        task = _make_task(task_id="TASK-001", required_capabilities=["python"])
        snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=["python"])
        manifest_before = deepcopy(manifest)

        allocator = TaskAllocator()
        allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert manifest == manifest_before


# --------------------------------------------------------------------------- #
# 参数校验
# --------------------------------------------------------------------------- #


class TestArgumentValidation:
    """``choose_claim`` 入参校验。"""

    def test_empty_node_id_raises_argument_error(self) -> None:
        """``node_id`` 为空抛 ArgumentError。"""
        snapshot = _make_snapshot(tasks=[])
        manifest = _make_manifest()

        allocator = TaskAllocator()
        with pytest.raises(ArgumentError, match="node_id"):
            allocator.choose_claim(snapshot, "", manifest)

    def test_empty_node_capabilities_treated_as_no_capabilities(self) -> None:
        """``node_capabilities.capabilities`` 为空列表时视为无能力。"""
        # 任务无 required_capabilities → 匹配任意节点（包括无能力的）。
        task = _make_task(task_id="TASK-001", required_capabilities=[])
        snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=[])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert decision["task_id"] == "TASK-001"

    def test_missing_capabilities_field_treated_as_empty(self) -> None:
        """``node_capabilities`` 缺失 ``capabilities`` 字段时视为空集合。"""
        task_with_caps = _make_task(task_id="TASK-001", required_capabilities=["python"])
        snapshot = _make_snapshot(tasks=[task_with_caps])
        manifest = _make_manifest()
        del manifest["capabilities"]  # 模拟缺失

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        # 节点无 capabilities 字段 → 视为空集合 → 不能满足 ['python']。
        assert decision["task_id"] is None
        assert decision["reason"] == "no_matching_tasks"


# --------------------------------------------------------------------------- #
# ClaimDecision 数据结构
# --------------------------------------------------------------------------- #


class TestClaimDecisionStructure:
    """``ClaimDecision`` 字段与契约一致。"""

    def test_claim_decision_granted_has_all_fields(self) -> None:
        """成功分配的 ClaimDecision 含全部字段。"""
        task = _make_task(task_id="TASK-001", required_capabilities=[])
        snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=["python"])

        allocator = TaskAllocator()
        decision = allocator.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert set(decision.keys()) == {
            "task_id",
            "node_id",
            "reason",
            "assignment_epoch",
        }
        assert decision["task_id"] == "TASK-001"
        assert decision["node_id"] == _NODE_ID_A
        assert decision["reason"] == "claim_granted"
        assert isinstance(decision["assignment_epoch"], int)
        assert decision["assignment_epoch"] >= 1

    def test_reason_values_are_stable_strings(self) -> None:
        """``reason`` 取稳定字符串（``claim_granted`` / ``no_matching_tasks``）。"""
        allocator = TaskAllocator()
        # 成功分配的 reason。
        task = _make_task(task_id="TASK-001", required_capabilities=[])
        snapshot_with_task = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=["python"])
        granted = allocator.choose_claim(snapshot_with_task, _NODE_ID_A, manifest)
        assert granted["reason"] == TaskAllocator.REASON_CLAIM_GRANTED
        assert granted["reason"] == "claim_granted"

        # 无匹配的 reason。
        empty_snapshot = _make_snapshot(tasks=[])
        no_match = allocator.choose_claim(empty_snapshot, _NODE_ID_A, manifest)
        assert no_match["reason"] == TaskAllocator.REASON_NO_MATCHING_TASKS
        assert no_match["reason"] == "no_matching_tasks"


# --------------------------------------------------------------------------- #
# 跨实例独立性
# --------------------------------------------------------------------------- #


class TestAllocatorIsolation:
    """多个 allocator 实例状态独立。"""

    def test_two_allocators_have_independent_epochs(self) -> None:
        """两个 allocator 实例的 epoch 计数器独立。"""
        task = _make_task(task_id="TASK-001", required_capabilities=[])
        snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=["python"])

        allocator_a = TaskAllocator()
        allocator_b = TaskAllocator()

        d_a1 = allocator_a.choose_claim(snapshot, _NODE_ID_A, manifest)
        d_b1 = allocator_b.choose_claim(snapshot, _NODE_ID_B, manifest)
        d_a2 = allocator_a.choose_claim(snapshot, _NODE_ID_A, manifest)

        # 两个 allocator 各自从 1 开始。
        assert d_a1["assignment_epoch"] == 1
        assert d_b1["assignment_epoch"] == 1
        # allocator_a 第二次分配 epoch=2。
        assert d_a2["assignment_epoch"] == 2

    def test_allocator_with_custom_initial_epoch_isolates(self) -> None:
        """``initial_epoch`` 不同的两个 allocator 独立计数。"""
        task = _make_task(task_id="TASK-001", required_capabilities=[])
        snapshot = _make_snapshot(tasks=[task])
        manifest = _make_manifest(capabilities=["python"])

        allocator_low = TaskAllocator(initial_epoch=0)
        allocator_high = TaskAllocator(initial_epoch=1000)

        d_low = allocator_low.choose_claim(snapshot, _NODE_ID_A, manifest)
        d_high = allocator_high.choose_claim(snapshot, _NODE_ID_A, manifest)

        assert d_low["assignment_epoch"] == 1
        assert d_high["assignment_epoch"] == 1001
