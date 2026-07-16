"""在指定工作区和隔离 Profile 中执行一个 Attempt 的接口。"""

from maf_contracts.job import AttemptResult, TaskDispatchEnvelope


async def execute_attempt(envelope: TaskDispatchEnvelope, workspace_path: str) -> AttemptResult:
    """构建只含获授权能力的 AgentContext，运行有界 Agent Loop 并封装结果。

    不接受额外 Role/Skill/Tool/Model ID；配置来自 control 任务、仓库版本文件和节点本地映射。异常必须
    映射为 NormalizedError，部分输出只有通过 Artifact 校验后才可列入结果。
    """
    ...
