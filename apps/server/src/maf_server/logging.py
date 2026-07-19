"""Server-side logging entry point.

Server 端日志初始化入口。``bootstrap.py`` 在进程启动时调用
``setup_logging(settings)``，把 ``ServerSettings.log_level`` 透传给
``maf_observability.configure_logging``，并绑定顶层 logger 名 ``maf_server``。

业务模块通过 ``get_logger(__name__)`` 取得结构化 logger；日志自动带上
``CorrelationContext`` 中的 ``trace_id``、``request_id``、``run_id``、
``event_id``、``assignment_epoch`` 等关联字段，并经脱敏 processor 处理
API Key、Token、密码与宿主机敏感路径。

本文件不打开日志文件、不连接网络；文件 handler 由部署脚本或
``logging.config.dictConfig`` 在更高层装配。``log_file`` 字段当前仅作为
未来扩展的占位（见 ``ServerSettings.log_file``）。
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
    from maf_server.config import ServerSettings

__all__ = [
    "CorrelationContext",
    "Logger",
    "correlation_context",
    "get_logger",
    "new_trace_id",
    "setup_logging",
]

#: Server 进程顶层 logger 名称。所有 ``maf_server.*`` 子 logger 共享 level。
_SERVER_LOGGER_NAME = "maf_server"


def setup_logging(settings: ServerSettings) -> None:
    """根据 ``ServerSettings`` 装配 structlog 与 stdlib ``logging``。

    读取 ``settings.log_level``（已由 ``ServerSettings`` 校验为大写合法值）
    并调用 ``maf_observability.configure_logging``。本函数幂等，可在测试中
    反复调用以重置 level。

    副作用：
        - 设置 ``structlog.configure`` 的 processor 链；
        - 设置 stdlib ``logging`` root 与 ``maf_server`` logger 的 level。
    """
    _configure_logging(level=settings.log_level, logger_name=_SERVER_LOGGER_NAME)


def get_logger(name: str | None = None, **initial_values: object) -> Logger:
    """返回绑定 ``name`` 的结构化 logger。

    通常传入模块 ``__name__``（如 ``maf_server.modules.runs.service``）。
    日志输出自动带上 ``CorrelationContext`` 快照与脱敏处理。
    """
    return _get_logger(name, **initial_values)
