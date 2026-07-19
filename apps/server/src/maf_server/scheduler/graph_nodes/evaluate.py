"""按 Artifact Contract 与 Gate Policy 评价完成 Attempt 的图节点接口。"""

from maf_server.scheduler.state import RunState


async def evaluate_node(state: RunState, node_key: str, attempt_id: str, quality_gate: object) -> RunState:
    """读取已持久 AttemptResult/Artifact/Review，调用确定性 Gate 并写 decision 引用。

    输入 attempt 必须属于当前等待节点且为最新有效 Attempt；缺失/Schema 失败不能视为 PASS。
    输出只加入 gate decision/review/artifact IDs，不把报告正文放入 checkpoint。
    """
    if not isinstance(state, RunState):
        state = RunState.from_value(state)
    if state.current_node_ids and node_key not in state.current_node_ids:
        raise ValueError("attempt does not belong to the current node")
    if not attempt_id:
        raise ValueError("attempt_id is required")
    # Gate implementations in the repository accept keyword-only definitions;
    # test doubles commonly expose a compact evaluate(attempt_id) method.  Try
    # the richer contract first and preserve deterministic result references.
    try:
        result = quality_gate.evaluate(attempt_id, node_key=node_key, run_id=state.run_id)  # type: ignore[attr-defined]
    except TypeError:
        gate_defs = state.metadata.get("gate_definitions", [])
        artifact_id = state.artifact_ids[-1] if state.artifact_ids else attempt_id
        try:
            result = quality_gate.evaluate(artifact_id, gate_definitions=gate_defs, actor_id="scheduler")  # type: ignore[attr-defined]
        except TypeError:
            result = quality_gate.evaluate(attempt_id)  # type: ignore[attr-defined]
    if hasattr(result, "__await__"):
        result = await result
    if result is None:
        raise ValueError("quality gate returned no decision")
    if isinstance(result, dict):
        decision_id = str(result.get("id") or result.get("decision_id") or f"gate:{state.run_id}:{node_key}:{attempt_id}")
        blocking_failed = any(
            isinstance(item, dict) and item.get("blocking") is True and item.get("passed") is not True
            for item in result.get("gate_results", [])
        )
        passed = bool(result.get("passed", result.get("status") in {"PASS", "APPROVED"})) and not blocking_failed
        overall = str(result.get("overall_status", result.get("status", "")))
    else:
        decision_id = f"gate:{state.run_id}:{node_key}:{attempt_id}"
        passed = bool(getattr(result, "passed", False))
        overall = str(getattr(result, "overall_status", ""))
    if decision_id not in state.gate_decision_ids:
        state.gate_decision_ids.append(decision_id)
    state.waiting_for = None
    if passed:
        state.status = "RUNNING"
    elif overall in {"WAITING_HUMAN", "PENDING"}:
        state.status = "WAITING_HUMAN"
        state.waiting_for = "HUMAN_GATE"
    elif overall in {"CHANGES_REQUESTED", "REWORK"}:
        state.status = "REWORK"
    else:
        state.status = "FAILED"
    state.attempt = max(state.attempt, 1)
    return state
