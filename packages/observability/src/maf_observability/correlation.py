"""Correlation context backed by ``contextvars``.

关联 ID 上下文，覆盖 HTTP 请求与 Git 协调事件两条链路：

- HTTP 请求链路：``trace_id``、``request_id``、``actor_id``、``organization_id``；
- Run/Task/Attempt 业务链路：``run_id``、``task_id``、``attempt_id``；
- Git 协调事件链路（见《GitHub 分布式协作协议》第 7 节）：``event_id``、
  ``node_id``、``assignment_id``、``assignment_epoch``、``control_commit``。

``assignment_epoch`` 是分布式 fencing token，不能被时间戳替代；此处仅作为
关联字段透传，不参与 fencing 校验。``control_commit`` 对应节点事件的
``based_on_control_commit``，用于追踪节点事件基于哪个 control 提交。

使用 ``contextvars.ContextVar`` 实现异步安全的上下文传播：同一 async task
或线程内的日志自动带上相同关联 ID，跨 task 边界由 ``contextvars`` 自动复制。
``correlation_context(...)`` 是上下文管理器，进入时绑定字段、退出时恢复。
"""

from __future__ import annotations

import contextlib
import uuid
from contextvars import ContextVar
from typing import Any, Final, Iterator

# --------------------------------------------------------------------------- #
# 关联字段定义
# --------------------------------------------------------------------------- #

#: HTTP 请求链路关联字段。
_TRACE_FIELDS: Final[tuple[str, ...]] = (
    "trace_id",
    "request_id",
    "actor_id",
    "organization_id",
)

#: Run/Task/Attempt 业务链路关联字段。
_RUN_FIELDS: Final[tuple[str, ...]] = (
    "run_id",
    "task_id",
    "attempt_id",
)

#: Git 协调事件链路关联字段（协议第 7 节）。
_GIT_FIELDS: Final[tuple[str, ...]] = (
    "event_id",
    "node_id",
    "assignment_id",
    "assignment_epoch",
    "control_commit",
)

#: 全部关联字段（按 trace → run → git 顺序）。
CORRELATION_FIELDS: Final[tuple[str, ...]] = _TRACE_FIELDS + _RUN_FIELDS + _GIT_FIELDS


def new_trace_id() -> str:
    """生成新的 ``trace_id``（UUID4 hex 字符串）。

    用于在请求入口或事件处理入口开启新的追踪链路。
    """
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# ContextVar 存储
# --------------------------------------------------------------------------- #

# 使用单个 dict ContextVar 而非每字段一个 ContextVar，便于原子快照与重置。
# 默认值为空 dict，表示无关联字段绑定。值为 ``Any`` 以同时支持字符串 ID
# （trace_id、event_id 等）与数值字段（assignment_epoch 为 int）。
_CTX: ContextVar[dict[str, Any]] = ContextVar(
    "maf_observability_correlation", default={}
)


def _current() -> dict[str, Any]:
    """返回当前上下文快照的浅拷贝（不会修改原始 dict）。"""
    return dict(_CTX.get())


def _coerce(value: Any) -> Any:
    """把传入值规范化为可序列化的标量。

    字符串、整数、浮点、布尔原样保留；其他类型转为字符串，保证日志可序列化。
    """
    if isinstance(value, str | int | float | bool):
        return value
    return str(value)


# --------------------------------------------------------------------------- #
# CorrelationContext
# --------------------------------------------------------------------------- #


class CorrelationContext:
    """关联 ID 上下文。

    通过 ``correlation_context(...)`` 上下文管理器进入；同一上下文内的日志
    自动带上绑定的关联字段。``snapshot()`` 返回当前快照，供 processor 注入
    到结构化日志事件中。

    本类不与 ``structlog`` 直接耦合，可独立用于任何需要关联 ID 的场景
    （如 audit_events、execution_events 表写入）。

    线程/异步安全：底层使用 ``contextvars.ContextVar``，同一 async task 内
    传播，跨 task 由 ``contextvars`` 自动复制。
    """

    __slots__ = ()

    @staticmethod
    def snapshot() -> dict[str, Any]:
        """返回当前上下文中已绑定的关联字段（非空值）。

        返回的 dict 是拷贝，修改不影响当前上下文。空上下文返回空 dict。
        """
        return {k: v for k, v in _current().items() if v}

    @staticmethod
    def get(field: str) -> Any | None:
        """返回单个关联字段值，未绑定时返回 ``None``。"""
        if field not in CORRELATION_FIELDS:
            raise KeyError(f"unknown correlation field: {field!r}")
        return _current().get(field)

    @staticmethod
    def bind(**fields: Any | None) -> None:
        """在当前上下文上增量绑定关联字段。

        已存在的字段会被覆盖。传入 ``None`` 会清除该字段。
        字符串与整数均可（``assignment_epoch`` 为整数）；其他类型会被
        转为字符串以保持日志可序列化。
        """
        unknown = set(fields) - set(CORRELATION_FIELDS)
        if unknown:
            raise KeyError(f"unknown correlation fields: {sorted(unknown)}")
        current = _current()
        for key, value in fields.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = _coerce(value)
        _CTX.set(current)

    @staticmethod
    def clear() -> None:
        """清除当前上下文中的所有关联字段。"""
        _CTX.set({})


def _merged(fields: dict[str, Any | None]) -> dict[str, Any]:
    """返回当前上下文合并 ``fields`` 后的 dict（None 清除字段）。"""
    current = _current()
    for key, value in fields.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = _coerce(value)
    return current


@contextlib.contextmanager
def correlation_context(**fields: Any | None) -> Iterator[dict[str, Any]]:
    """绑定关联字段的上下文管理器。

    进入时合并 ``fields`` 到当前上下文；退出时恢复到进入前的状态。
    允许嵌套：内层上下文只增加字段，不影响外层。

    Example::

        with correlation_context(trace_id=new_trace_id(), run_id="r-1"):
            log = get_logger()
            log.info("run_started")  # 自动带上 trace_id, run_id

    传入 ``None`` 值会清除该字段（在当前上下文层）。
    """
    unknown = set(fields) - set(CORRELATION_FIELDS)
    if unknown:
        raise KeyError(f"unknown correlation fields: {sorted(unknown)}")
    token = _CTX.set(_merged(fields))
    try:
        yield CorrelationContext.snapshot()
    finally:
        _CTX.reset(token)


__all__ = [
    "CORRELATION_FIELDS",
    "CorrelationContext",
    "correlation_context",
    "new_trace_id",
]
