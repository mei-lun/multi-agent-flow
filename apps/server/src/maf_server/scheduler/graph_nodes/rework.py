"""创建有界返工上下文且不修改旧 Artifact 的图节点接口。"""

from maf_server.scheduler.state import RunState


async def prepare_rework(state: RunState, gate_decision_id: str, target_node_key: str) -> RunState:
    """检查 run/node/category 返工次数未超限，创建新的 Task 输入 Bundle 引用。

    Bundle 包含阻断项、证据和旧输出版本，但旧 Artifact 保持不变。target 必须来自 Workflow
    显式 rework mapping，不能由模型任意指定。超限时转失败或人工门禁。
    """
    ...
