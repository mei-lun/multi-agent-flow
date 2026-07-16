"""进程内发布、持久事件与 Outbox 接口。"""

from typing import AsyncIterator, Protocol
from maf_contracts.events import DomainEvent


class EventPublisher(Protocol):
    async def append(self, event: DomainEvent) -> None:
        """在当前 UnitOfWork 内把领域事件和 Outbox 一起写入，event_id 必须唯一。"""
        ...
    async def publish_pending(self, batch_size: int = 100) -> int:
        """租用未发布 Outbox，调用本地消费者，成功后标记完成；失败保留重试。"""
        ...
    async def subscribe_run(self, run_id: str, after_event_id: str | None) -> AsyncIterator[DomainEvent]:
        """先回放持久事件再订阅新事件，供 SSE 使用。"""
        ...

