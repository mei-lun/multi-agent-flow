"""按 Artifact Contract 与 Gate Policy 评价完成 Attempt 的图节点接口。"""

from maf_server.scheduler.state import RunState


async def evaluate_node(state: RunState, node_key: str, attempt_id: str, quality_gate: object) -> RunState:
    """读取已持久 AttemptResult/Artifact/Review，调用确定性 Gate 并写 decision 引用。

    输入 attempt 必须属于当前等待节点且为最新有效 Attempt；缺失/Schema 失败不能视为 PASS。
    输出只加入 gate decision/review/artifact IDs，不把报告正文放入 checkpoint。
    """
    ...
