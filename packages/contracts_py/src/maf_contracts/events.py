"""持久化与浏览器 SSE 共用的版本化领域事件。

根据《多 Agent 协同工具系统设计文档》§10.7 与《GitHub 分布式协作协议》：

- ``DomainEvent`` 是所有领域事件的统一 Envelope，与业务写入同一事务插入
  ``outbox_events`` 表，由后台协程读取并分发给本地消费者（投影/通知）。
- ``EventEnvelope`` 包装 ``DomainEvent`` 并附加 Outbox 消费状态（发布时间、
  重试计数、最近错误），用于查询未发布/已发布事件。
- Outbox 是本地投影/通知机制，不是跨节点事实源；Git coordination 事件才是
  跨节点事实源，由 Git 协议定义，本模块不与 Git coordination 事件语义混淆。
- 本文件只定义字段与序列化模型，不连接数据库、不发布事件。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class ActorRef(BaseModel):
    """事件发起者引用（设计文档 §10.7）。

    - ``actor_type``：``USER`` / ``AGENT`` / ``SERVICE``；
    - ``actor_id``：用户/Agent/服务的稳定标识。
    """

    actor_type: str
    actor_id: str


class DomainEvent(BaseModel):
    """领域事件 Envelope（设计文档 §10.7）。

    所有需要异步传播的领域事件由此基类承载。关键字段：

    - ``event_id``：全局唯一 UUID，用于 Outbox 消费幂等去重；
    - ``event_type`` / ``schema_version``：事件类型与负载 Schema 版本；
    - ``aggregate_type`` / ``aggregate_id``：事件所属聚合根类型与标识；
    - ``organization_id`` / ``project_id`` / ``run_id``：租户与归属范围，
      ``project_id``/``run_id`` 可空，用于按项目/运行查询事件流；
    - ``occurred_at``：事件发生时间（UTC，带时区）；
    - ``actor``：发起者引用；
    - ``trace_id``：链路追踪 ID；
    - ``payload``：事件负载，由 ``event_type`` 决定具体字段。

    Outbox 是本地投影/通知机制，不是跨节点事实源；跨节点事件由 Git coordination
    协议定义，本类型不与 Git coordination 事件语义混淆。
    """

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: str
    schema_version: int = 1
    aggregate_type: str
    aggregate_id: str
    organization_id: str
    project_id: str | None = None
    run_id: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    actor: ActorRef
    trace_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


#: Outbox 事件发布状态字面量。
EventStatus = Literal["PENDING", "PUBLISHED", "FAILED"]


class EventEnvelope(BaseModel):
    """Outbox 中事件的持久化包装，含消费状态与重试计数。

    对应 ``outbox_events`` 表的一行：领域事件本身（``event``）加上 Outbox 调度
    元数据（``published_at``、``publish_attempts``、``last_error``）。
    ``published_at`` 为 ``None`` 表示尚未发布（含可重试失败）。
    """

    event: DomainEvent
    published_at: datetime | None = None
    publish_attempts: int = 0
    last_error: str | None = None

    @property
    def status(self) -> EventStatus:
        """派生状态：已发布 / 失败（有错误且未发布）/ 待发布。"""
        if self.published_at is not None:
            return "PUBLISHED"
        if self.last_error is not None:
            return "FAILED"
        return "PENDING"


__all__ = ["ActorRef", "DomainEvent", "EventEnvelope", "EventStatus"]
