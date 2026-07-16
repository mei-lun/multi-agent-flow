"""校验 Git 权威分配并协调完整本地任务生命周期的接口。"""

from maf_contracts.coordination import CoordinationTask
from maf_contracts.job import AttemptResult


async def execute_job(task: CoordinationTask) -> AttemptResult:
    """执行一个已在 control 分配给本节点的任务，但不决定后续工作流。

    顺序：fetch control 并验证 owner/assignment_id/epoch/control commit；验证能力和镜像；准备
    任务分支独立工作区；创建受限容器；调用 execute_attempt；定期提交进度事件；打包输出并
    push 任务分支；返回 AttemptResult 供构造 SUBMISSION_CREATED。发现 epoch 变化时停止提交
    权威状态，但保留本地/远端旧分支供恢复。
    """
    ...
