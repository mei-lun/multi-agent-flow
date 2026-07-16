"""节点通过 Git pull/push 参与协调的接口。"""

from typing import Protocol

from maf_contracts.coordination import CoordinationEvent, CoordinationSnapshot, CoordinationTask


class GitCoordinationClient(Protocol):
    async def fetch_control(self) -> CoordinationSnapshot:
        """fetch 远端 `maf/control`，验证 fast-forward、协议版本和 Schema 后返回快照。

        不使用工作区未提交内容覆盖 control；签名/Schema/仓库身份异常时停止认领新任务。
        """
        ...

    async def append_event(self, event: CoordinationEvent) -> str:
        """向本节点 `maf/node/<node-id>` 分支追加一个事件文件并 push，返回事件 commit。

        只能 fast-forward 自己的节点分支；push 冲突时 fetch/rebase 并用同一 event_id 重试；
        不能直接修改 `maf/control`。
        """
        ...

    async def wait_for_assignment(self, task_id: str, event_id: str, timeout_seconds: int) -> CoordinationTask | None:
        """周期性 fetch control，直到 claim 被接受/拒绝或超时；仅 owner/epoch 匹配时返回任务。"""
        ...

    async def push_task_branch(self, task: CoordinationTask, workspace_path: str) -> str:
        """把本地提交 fast-forward push 到任务规定分支并返回远端 head。

        branch 名、base、node_id 和 epoch 必须与 control assignment 一致；禁止 push main/control。
        """
        ...

    async def current_task(self, task_id: str) -> CoordinationTask:
        """重新 fetch 后返回权威任务，用于检查取消、超时、返工和 epoch 变化。"""
        ...

