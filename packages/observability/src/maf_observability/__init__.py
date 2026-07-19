"""Multi-Agent Flow shared observability package.

提供统一的：

- 结构化日志（``structlog`` 包装，JSON Lines 输出）；
- 关联 ID 上下文（``CorrelationContext``，基于 ``contextvars`` 传播
  ``trace_id``/``event_id``/``assignment_epoch`` 等字段，覆盖 HTTP 请求
  与 Git 协调事件）；
- 敏感字段脱敏处理器（API Key、Token、密码、宿主机敏感路径）。

本包不依赖 FastAPI、SQLAlchemy、LangGraph、Docker 或模型 SDK；仅依赖
``structlog``。Server 与 Runner 的 ``logging.py`` 入口负责调用
``configure_logging`` 完成 processor 链装配，业务代码通过 ``get_logger``
取得结构化 logger。
"""

from __future__ import annotations

from maf_observability.correlation import (
    CORRELATION_FIELDS,
    CorrelationContext,
    correlation_context,
    new_trace_id,
)
from maf_observability.logger import (
    Logger,
    configure_logging,
    get_logger,
    make_processor_chain,
)
from maf_observability.redaction import (
    REDACTED_PLACEHOLDER,
    redact_sensitive,
)

__all__ = [
    "CORRELATION_FIELDS",
    "CorrelationContext",
    "Logger",
    "REDACTED_PLACEHOLDER",
    "configure_logging",
    "correlation_context",
    "get_logger",
    "make_processor_chain",
    "new_trace_id",
    "redact_sensitive",
]
