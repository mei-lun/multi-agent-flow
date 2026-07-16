"""Git 任务分配超时、失联和重新分配接口。"""


async def reconcile_assignments(project_id: str, now_iso: str, batch_size: int = 100) -> int:
    """检查有限批 ASSIGNED/IN_PROGRESS 任务并返回发生状态变化数量。

    先 fetch 最新 control、node events 和任务分支；有效进度或新 commit 会刷新判断；超过超时
    与宽限后写 LEASE_EXPIRED。重新分配必须增加 assignment_epoch，旧节点事件随后被 fencing
    拒绝。方法不删除旧工作分支。
    """
    ...

