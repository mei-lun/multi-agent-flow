"""Git 单写协调、节点事件消费和 SQLite 投影接口。"""

from typing import Protocol

from maf_contracts.coordination import CoordinationEvent, CoordinationTask, EventDecision, NodeManifest
from .schemas import SyncResult


class GitCoordinationService(Protocol):
    async def initialize_project(self, repository_binding_id: str, project_id: str) -> str:
        """在仓库创建或验证 `maf/control` 与 `.maf` 协议文件，返回初始 control commit。

        只允许在空协议或兼容版本上初始化；已有不兼容协议必须停止。中央调度器是唯一写者，
        初始化不得修改 main 上的业务代码。
        """
        ...

    async def publish_tasks(self, project_id: str, tasks: list[CoordinationTask], expected_control_commit: str) -> str:
        """校验依赖、ID、Schema 和 expected head 后写入独立 task 文件并生成 status.md。

        push 使用 fast-forward；远端 head 不匹配时重新 fetch/reconcile，禁止 force push。成功返回
        新 control commit，确认远端可见后才能推进 SQLite 投影水位。
        """
        ...

    async def register_node_event(self, event: CoordinationEvent) -> EventDecision:
        """验证 NODE_REGISTERED/NODE_UPDATED 事件的分支所有者、签名、能力和 Schema。

        接受后由中央调度器写 nodes/<node-id>.yaml；节点不能直接写 control。重复 event_id 返回
        首次决定。
        """
        ...

    async def process_event(self, event: CoordinationEvent) -> EventDecision:
        """处理认领、进度、阻塞、提交或放弃事件。

        依次校验事件分支/签名、event_id、based_on_control_commit、节点状态、任务当前状态、
        assignment_id/epoch 和允许的状态转换；接受后更新 task/event/status 并 push control。
        旧 epoch 事件只能记录为拒绝，不能覆盖当前任务。
        """
        ...

    async def sync(self, project_id: str) -> SyncResult:
        """fetch control 和全部 `maf/node/*`，按确定顺序处理未消费事件并更新 SQLite 投影。

        节点分支仅接受 fast-forward 历史；事件按 occurred_at 只用于显示，处理顺序使用 Git 可达
        顺序和 event_id。单次失败保留 projector 水位供重放。
        """
        ...

    async def reconcile_expired_assignments(self, project_id: str, now: str) -> list[str]:
        """检查长时间无有效进度的 ASSIGNED/IN_PROGRESS 任务。

        先 fetch 任务分支确认是否有未报告提交，再经过宽限期；失效时写 LEASE_EXPIRED、清空
        owner，并在重新分配时递增 assignment_epoch。返回发生变化的 task IDs。
        """
        ...

    async def rebuild_projection(self, project_id: str) -> str:
        """从 control 当前任务/节点和 canonical events 重建 SQLite，返回投影水位 commit。"""
        ...


class TaskAllocator(Protocol):
    async def choose_claim(self, task: CoordinationTask, candidates: list[CoordinationEvent], nodes: dict[str, NodeManifest]) -> CoordinationEvent | None:
        """在合法申请中按能力、容量、优先级和稳定 tie-break 选择一个；无候选返回 None。

        该函数必须确定性，不能用 LLM 随机选择。同一 control commit 与候选集合应得到相同结果。
        """
        ...

