"""Run 和人工命令的应用接口。调度推进由 SchedulerService 完成。"""

from typing import AsyncIterator, Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class RunService(Protocol):
    async def start_run(self, actor: ActorContext, project_id: str, request: StartRunRequest) -> RunView:
        """创建可恢复 Run 并请求 Scheduler 启动。

        实现顺序：校验权限和项目 ACTIVE；确认 Workflow/Role/Skill/Policy 均为已发布精确
        版本；确认项目输入与仓库绑定属于项目；限制不得超过项目和系统上限；检查幂等键；
        构建完整 Run Snapshot Artifact；在事务中创建 Run/事件；提交后调用 Scheduler。
        Scheduler 启动失败不删除 Run，由恢复任务继续。成功返回 CREATED/RUNNING 投影。
        """
        ...

    async def get_run(self, actor: ActorContext, run_id: str) -> RunView:
        """返回持久化投影；不得通过读取 LangGraph 内部对象临时拼接 HTTP 响应。"""
        ...

    async def get_graph(self, actor: ActorContext, run_id: str) -> RunGraphView:
        """返回节点/边运行投影和 projection_version，供前端绘图。"""
        ...

    async def list_tasks(self, actor: ActorContext, run_id: str, query: TaskQuery) -> TaskPage:
        """分页返回 Task 及 Attempt 摘要；大日志以 Artifact 或事件流引用提供。"""
        ...

    async def stream_events(
        self, actor: ActorContext, run_id: str, last_event_id: str | None
    ) -> AsyncIterator[RunEventView]:
        """按 event_id 顺序发送 SSE。

        检查项目权限；从 last_event_id 后回放持久事件，再订阅新事件；定期发送 heartbeat；
        慢客户端使用有界缓冲并断开重连，不能阻塞事件写入。
        """
        ...

    async def pause(self, actor: ActorContext, run_id: str, request: RunCommand) -> CommandResult:
        """请求暂停 Run。

        CREATED/RUNNING/WAITING_HUMAN 可受理；先记录幂等命令和 PAUSE_REQUESTED，再通知
        Scheduler 停止分派新节点。运行中的 Attempt 按策略结束或取消，最终转 PAUSED。
        """
        ...

    async def resume(self, actor: ActorContext, run_id: str, request: ResumeRunRequest) -> CommandResult:
        """从 PAUSED 或已满足人工条件的 WAITING_HUMAN 恢复。

        验证未过总时限、预算可用、相关 human_decision 已提交；记录命令后由 Scheduler 从
        checkpoint 恢复，禁止重新创建 Run。
        """
        ...

    async def cancel(self, actor: ActorContext, run_id: str, request: RunCommand) -> CommandResult:
        """幂等取消未结束 Run。

        先转 CANCELLING，停止新 job，将已租约 job 标记 cancel_requested；所有 Attempt 收敛
        后转 CANCELLED。重复取消返回同一终态；已 COMPLETED/FAILED 不可改写。
        """
        ...

    async def increase_budget(self, actor: ActorContext, run_id: str, request: IncreaseBudgetRequest) -> RunView:
        """经权限检查追加预算，不重置已消费值。

        货币必须与原预算一致；追加后仍不得超过系统硬上限；写 budget.increased 事件，等待
        预算的 Run 可由 Scheduler 再评估。
        """
        ...

    async def retry_task(self, actor: ActorContext, task_id: str, request: RetryTaskRequest) -> TaskView:
        """人工创建新的 Attempt，不复用旧 Attempt ID。

        仅可重试失败/丢失/被拒绝的 Task；验证快照和指定输入 Artifact；增加 attempt_no，
        保存操作者与原因并唤醒 Scheduler。旧结果保留用于审计。
        """
        ...

