"""Git 协调事实和 SQLite 投影水位接口。"""

from typing import Protocol

from maf_contracts.coordination import CoordinationEvent, CoordinationSnapshot, EventDecision
from .schemas import ProjectorState


class GitCoordinationRepository(Protocol):
    async def get_projector_state(self, repository_binding_id: str) -> ProjectorState | None:
        """读取当前投影水位和错误状态；不存在表示尚未初始化。"""
        ...

    async def project_snapshot(self, snapshot: CoordinationSnapshot, expected_previous_commit: str | None) -> None:
        """在一个 SQLite 事务中替换/更新任务、节点投影并推进 control commit 水位。

        expected_previous_commit 不匹配时拒绝，避免两个 projector 乱序覆盖。
        """
        ...

    async def has_processed_event(self, event_id: str) -> bool:
        """查询事件去重记录。"""
        ...

    async def record_event_decision(self, event: CoordinationEvent, decision: EventDecision) -> None:
        """保存接受/拒绝决定和原因；同 event_id 不可产生不同决定。"""
        ...

