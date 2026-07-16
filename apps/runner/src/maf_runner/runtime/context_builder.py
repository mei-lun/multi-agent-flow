"""从 Role/Workflow 快照构建不可变 AgentContext 的接口。"""

from typing import Any, Protocol
from maf_contracts.job import TaskDispatchEnvelope


class ContextBuilder(Protocol):
    async def build(self, envelope: TaskDispatchEnvelope, workspace_path: str) -> dict[str, Any]:
        """构造 Agent Loop 唯一上下文。

        校验 control commit、任务分支 base 和输入 hash；从仓库读取精确 Skill Version；从节点
        本地注册表取得允许 Tool/Model 映射；放入 Prompt、任务说明、输出 Contract、预算和取消
        信号。不得添加节点本机拥有但 envelope
        未授权的 Skill/Tool/Model；大输入只放索引和按需读取句柄。
        """
        ...
