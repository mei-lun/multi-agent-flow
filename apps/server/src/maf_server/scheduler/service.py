"""启动、恢复和控制 LangGraph Run 的接口。"""

from typing import Any, Protocol


class SchedulerService(Protocol):
    async def start_run(self, run_id: str) -> None:
        """为 CREATED Run 建立首个 checkpoint 并推进到第一个持久等待点。

        读取不可变 Run Snapshot，编译对应 workflow hash 的图；使用 run_id 作为 thread key；
        若 checkpoint 已存在则按恢复处理，不能重复创建 Task；推进过程中所有外部执行只转成
        Git coordination task。失败要记录可恢复调度错误，不能删除 Run。
        """
        ...

    async def resume_run(self, run_id: str, command: dict[str, Any]) -> None:
        """从最新 checkpoint 处理一个幂等唤醒命令。

        command 必须包含唯一 event/command ID；先检查 wakeup 去重表，再加载 checkpoint 和
        最新领域状态；输入结果后推进到下一持久等待点。重复命令不重复产生 Task/Job。
        """
        ...

    async def pause_run(self, run_id: str, command_id: str) -> None:
        """停止向 control 发布新任务，在安全点把 Run 收敛为 PAUSED。已分配节点通过下一次 fetch 看到取消事件。"""
        ...

    async def cancel_run(self, run_id: str, command_id: str) -> None:
        """在 control 标记未开始任务取消、请求进行中任务停止，并最终收敛为 CANCELLED。"""
        ...

    async def recover_incomplete_runs(self) -> None:
        """服务启动后扫描非终态 Run，并根据 checkpoint/等待原因恢复。

        必须分批和加互斥，跳过正常等待 Git 任务提交/人工的 Run；只修复领域状态与 checkpoint
        不一致或有未消费 wakeup 的 Run。
        """
        ...

    async def handle_task_submission(self, task_id: str, submission_event_id: str) -> None:
        """确认提交事件已进入 control 且分支/epoch 有效后，幂等唤醒对应 Run。"""
        ...

    async def handle_human_decision(self, inbox_item_id: str) -> None:
        """读取不可变 Decision 并唤醒等待该 subject/version 的 Run。"""
        ...
