"""把 Workflow 节点转换为 Git 协调任务的接口。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    run_id: str
    node_run_id: str
    role_snapshot_id: str
    repository_binding_id: str
    required_capabilities: tuple[str, ...]
    base_commit: str


class TaskDispatcher:
    async def dispatch(self, request: DispatchRequest) -> str:
        """创建或返回一个 Git coordination task_id。

        从 Run Snapshot 构造功能级任务、输入引用、输出契约、能力和依赖；以
        run_id+node_run_id 为幂等键；通过 GitCoordinationService 写 `maf/control`。该方法不
        选择节点、不调用节点，也不创建 HTTP Job。
        """
        ...

