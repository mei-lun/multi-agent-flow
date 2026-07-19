"""创建有界返工上下文且不修改旧 Artifact 的图节点接口。"""

from maf_server.scheduler.state import RunState


async def prepare_rework(state: RunState, gate_decision_id: str, target_node_key: str) -> RunState:
    """检查 run/node/category 返工次数未超限，创建新的 Task 输入 Bundle 引用。

    Bundle 包含阻断项、证据和旧输出版本，但旧 Artifact 保持不变。target 必须来自 Workflow
    显式 rework mapping，不能由模型任意指定。超限时转失败或人工门禁。
    """
    if not isinstance(state, RunState):
        state = RunState.from_value(state)
    if not gate_decision_id or not target_node_key:
        raise ValueError("gate_decision_id and target_node_key are required")
    mappings = state.metadata.get("rework_mappings", {})
    if isinstance(mappings, dict) and mappings:
        allowed = mappings.get(gate_decision_id, mappings.get("default", []))
        if isinstance(allowed, str):
            allowed = [allowed]
        if target_node_key not in allowed:
            raise ValueError("rework target is not allowed by the published workflow")
    limit = state.metadata.get("max_reworks", 3)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 0
    count = state.rework_counts.get(target_node_key, 0)
    prefix = f"rework:{state.run_id}:{target_node_key}:"
    # Replaying the same gate decision must not consume another rework slot.
    if any(item.startswith(prefix) and item.endswith(f":{gate_decision_id}") for item in state.artifact_ids):
        state.current_node_ids = [target_node_key]
        state.status = "READY"
        state.waiting_for = None
        return state
    if count >= limit:
        state.status = "FAILED"
        state.waiting_for = None
        return state
    state.rework_counts[target_node_key] = count + 1
    bundle_id = f"rework:{state.run_id}:{target_node_key}:{count + 1}:{gate_decision_id}"
    if bundle_id not in state.artifact_ids:
        state.artifact_ids.append(bundle_id)
    state.current_node_ids = [target_node_key]
    state.status = "READY"
    state.waiting_for = None
    return state
