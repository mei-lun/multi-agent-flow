"""Git 任务分配超时、失联和重新分配接口。"""

import inspect


async def reconcile_assignments(project_id: str, now_iso: str, batch_size: int = 100, *, repository: object | None = None, coordination_service: object | None = None) -> int:
    """检查有限批 ASSIGNED/IN_PROGRESS 任务并返回发生状态变化数量。

    先 fetch 最新 control、node events 和任务分支；有效进度或新 commit 会刷新判断；超过超时
    与宽限后写 LEASE_EXPIRED。重新分配必须增加 assignment_epoch，旧节点事件随后被 fencing
    拒绝。方法不删除旧工作分支。
    """
    if not project_id:
        raise ValueError("project_id is required")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    # The concrete Git projector can provide the complete atomic operation.
    # Keeping this adapter small also allows deterministic fakes in replay tests.
    target = repository or coordination_service
    if target is None:
        return 0
    method = getattr(target, "reconcile_assignments", None)
    if method is None:
        method = getattr(target, "list_expired_assignments", None)
        if method is None:
            return 0
        rows = method(project_id, now_iso, batch_size)
        if inspect.isawaitable(rows):
            rows = await rows
        changed = 0
        for row in rows or []:
            expire = getattr(target, "expire_assignment", None)
            if expire is None:
                continue
            result = expire(row)
            if inspect.isawaitable(result):
                result = await result
            changed += int(result is not False)
        return changed
    result = method(project_id, now_iso, batch_size)
    if inspect.isawaitable(result):
        result = await result
    return int(result or 0)
