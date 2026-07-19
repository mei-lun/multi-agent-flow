"""Runner-side logging entry point.

Runner 端日志初始化入口。``main.py`` 在进程启动时调用
``setup_logging(settings)``，把 ``NodeSettings.log_level`` 透传给
``maf_observability.configure_logging``，并绑定顶层 logger 名 ``maf_runner``。

Runner 不自建 HTTP 控制面（见《GitHub 分布式协作协议》第 1 节），日志关联 ID
覆盖 Git 事件链路：``node_id``、``event_id``、``assignment_id``、
``assignment_epoch``、``control_commit``，以及 Run/Task/Attempt 业务链路。
这些字段通过 ``CorrelationContext`` 在 fetch control、claim、progress、
submit 各阶段传播，使同一 Git 事件的处理日志可被追踪。

业务模块通过 ``get_logger(__name__)`` 取得结构化 logger；日志经脱敏
processor 处理 Git credentials token、capability signing key 文件路径与
宿主机敏感路径。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from maf_observability import (
    CorrelationContext,
    Logger,
    configure_logging as _configure_logging,
    correlation_context,
    get_logger as _get_logger,
    new_trace_id,
)

if TYPE_CHECKING:
    from maf_runner.config import NodeSettings

__all__ = [
    "CorrelationContext",
    "Logger",
    "correlation_context",
    "get_logger",
    "new_trace_id",
    "setup_logging",
]

#: Runner 进程顶层 logger 名称。所有 ``maf_runner.*`` 子 logger 共享 level。
_RUNNER_LOGGER_NAME = "maf_runner"


def setup_logging(settings: NodeSettings) -> None:
    """根据 ``NodeSettings`` 装配 structlog 与 stdlib ``logging``。

    读取 ``settings.log_level``（已由 ``NodeSettings`` 校验为大写合法值）
    并调用 ``maf_observability.configure_logging``。本函数幂等，可在测试中
    反复调用以重置 level。

    副作用：
        - 设置 ``structlog.configure`` 的 processor 链；
        - 设置 stdlib ``logging`` root 与 ``maf_runner`` logger 的 level。
    """
    _configure_logging(level=settings.log_level, logger_name=_RUNNER_LOGGER_NAME)


def get_logger(name: str | None = None, **initial_values: object) -> Logger:
    """返回绑定 ``name`` 的结构化 logger。

    通常传入模块 ``__name__``（如 ``maf_runner.execute_job``）。
    日志输出自动带上 ``CorrelationContext`` 快照与脱敏处理。
    """
    return _get_logger(name, **initial_values)
