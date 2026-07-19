"""从 READY Workflow Node 创建持久 Git 协调任务的图节点接口。"""

from maf_server.scheduler.state import RunState


async def dispatch_node(state: RunState, node_key: str, dispatcher: object) -> RunState:
    """检查节点尚未分派，构造唯一 node_run_id，调用 TaskDispatcher 并返回 WAITING_GIT_TASK 状态。

    输入只含 RunState 和已编译 node_key；输出是新的小状态。重复执行必须找到已有 Task/Job，
    不创建第二条；不得在此等待远程节点或调用 Agent。
    """
    if not isinstance(state, RunState):
        state = RunState.from_value(state)
    existing = state.task_ids.get(node_key)
    if existing:
        state.waiting_for = existing
        state.status = "WAITING_GIT_TASK"
        return state

    node_run_id = f"{state.run_id}:{node_key}"
    metadata = state.metadata.get("nodes", {})
    node_meta = metadata.get(node_key, {}) if isinstance(metadata, dict) else {}
    if not isinstance(node_meta, dict):
        node_meta = {}
    from maf_server.scheduler.dispatcher import DispatchRequest

    request = DispatchRequest(
        run_id=state.run_id,
        node_run_id=node_run_id,
        role_snapshot_id=str(node_meta.get("role_snapshot_id", node_meta.get("role_version_id", ""))),
        repository_binding_id=str(node_meta.get("repository_binding_id", "")),
        required_capabilities=tuple(str(x) for x in node_meta.get("required_capabilities", ())),
        base_commit=str(node_meta.get("base_commit", "")),
    )
    task_id = await dispatcher.dispatch(request)  # type: ignore[attr-defined]
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("dispatcher.dispatch must return task_id")
    state.task_ids[node_key] = task_id
    state.current_node_ids = [node_key]
    state.waiting_for = task_id
    state.status = "WAITING_GIT_TASK"
    return state
