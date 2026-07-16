"""从 READY Workflow Node 创建持久 Git 协调任务的图节点接口。"""

from maf_server.scheduler.state import RunState


async def dispatch_node(state: RunState, node_key: str, dispatcher: object) -> RunState:
    """检查节点尚未分派，构造唯一 node_run_id，调用 TaskDispatcher 并返回 WAITING_GIT_TASK 状态。

    输入只含 RunState 和已编译 node_key；输出是新的小状态。重复执行必须找到已有 Task/Job，
    不创建第二条；不得在此等待远程节点或调用 Agent。
    """
    ...
