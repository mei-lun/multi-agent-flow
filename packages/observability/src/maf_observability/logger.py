"""Structured logger built on ``structlog``.

Logger 包装层，提供：

- ``configure_logging``：装配 structlog processor 链，输出 JSON Lines；
- ``get_logger``：返回绑定当前模块名的 ``structlog.BoundLogger``；
- ``make_processor_chain``：构造 processor 列表，供测试或自定义入口复用。

processor 链顺序（自上而下）：

1. ``structlog.contextvars.merge_contextvars`` —— 合并 structlog 自带的
   ContextVar（与 ``CorrelationContext`` 独立，预留给 structlog 原生 API）；
2. ``_inject_correlation`` —— 把 ``CorrelationContext.snapshot()`` 注入
   ``event_dict``，使每条日志自动带上 ``trace_id``、``event_id``、
   ``assignment_epoch`` 等关联字段；
3. ``structlog.processors.add_log_level`` —— 添加 ``level`` 字段；
4. ``structlog.processors.TimeStamper(fmt="iso", utc=True)`` ——
   ISO 8601 UTC 时间戳；
5. ``structlog.processors.StackInfoRenderer`` —— 渲染 ``stack_info``；
6. ``structlog.processors.format_exc_info`` —— 渲染 ``exc_info`` 为字符串；
7. ``redact_processor`` —— 脱敏敏感键名与宿主机敏感路径；
8. ``structlog.processors.JSONRenderer`` —— 序列化为 JSON 字符串。

整个链路无外部 I/O；输出由调用方通过 stdlib ``logging`` handler 或直接
捕获 ``structlog`` 输出处理。``configure_logging`` 默认把 stdlib
``logging`` 也配置为相同 level，使 ``structlog`` 与 stdlib logger 行为
一致。
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any, Final

import structlog
from structlog.typing import Processor

from maf_observability.correlation import CorrelationContext
from maf_observability.redaction import redact_processor

#: ``structlog`` 默认使用的 logger 名称前缀（与 stdlib ``logging`` 对齐）。
_DEFAULT_LOGGER_NAME: Final[str] = "maf"

#: 合法的日志 level 名称（与 ``ServerSettings.log_level`` 对齐）。
_VALID_LEVELS: Final[frozenset[str]] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)

#: ``structlog.BoundLogger`` 的类型别名，便于上层静态标注。
Logger = structlog.stdlib.BoundLogger


# --------------------------------------------------------------------------- #
# processor 链
# --------------------------------------------------------------------------- #


def _inject_correlation(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor：注入 ``CorrelationContext`` 快照。

    把当前上下文中已绑定的关联字段（``trace_id``、``event_id`` 等）合并到
    ``event_dict``。若 ``event_dict`` 已显式带同名字段，保留显式值
    （调用方显式 bind 优先）。
    """
    snapshot = CorrelationContext.snapshot()
    for key, value in snapshot.items():
        if key not in event_dict:
            event_dict[key] = value
    return event_dict


def make_processor_chain(*, keep_exc: bool = True) -> list[Processor]:
    """返回标准 structlog processor 链。

    参数：
        keep_exc：是否包含异常渲染 processor（``StackInfoRenderer`` +
            ``format_exc_info``）。测试场景下可关闭以避免输出依赖
            ``sys.exc_info``。

    返回的列表是拷贝，调用方可追加或裁剪。
    """
    chain: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        _inject_correlation,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    if keep_exc:
        chain.extend(
            [
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
            ]
        )
    chain.extend(
        [
            redact_processor,
            structlog.processors.JSONRenderer(),
        ]
    )
    return chain


# --------------------------------------------------------------------------- #
# 配置入口
# --------------------------------------------------------------------------- #


def configure_logging(
    *,
    level: str = "INFO",
    logger_name: str = _DEFAULT_LOGGER_NAME,
    extra_processors: list[Processor] | None = None,
) -> None:
    """装配 structlog 与 stdlib ``logging``。

    参数：
        level：日志 level 名称（``DEBUG``/``INFO``/``WARNING``/``ERROR``/
            ``CRITICAL``）。大小写不敏感，会被规范化为大写。
        logger_name：stdlib ``logging`` 顶层 logger 名称；默认 ``maf``。
            该 logger 与所有以 ``maf`` 开头的子 logger 共享 level。
        extra_processors：追加到标准链尾部的额外 processor（在
            ``JSONRenderer`` 之前插入无效；通常为空）。默认不追加。

    本函数幂等：可多次调用以重新配置 level。processor 链在每次调用时
    重建，避免跨测试污染。

    副作用：
        - 设置 ``structlog.configure``；
        - 设置 ``logging.basicConfig`` 与指定 logger 的 level；
        - 不打开文件、不连接网络。
    """
    upper_level = level.upper()
    if upper_level not in _VALID_LEVELS:
        raise ValueError(
            f"level must be one of {sorted(_VALID_LEVELS)}, got {level!r}"
        )

    processors = make_processor_chain()
    if extra_processors:
        processors.extend(extra_processors)

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            _level_to_int(upper_level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )

    # 同步 stdlib logging 的 level，使三方库通过 ``logging.getLogger`` 产出
    # 的日志也按统一 level 过滤。``force=True`` 移除既有 handler，避免重复输出。
    logging.basicConfig(
        level=_level_to_int(upper_level),
        format="%(message)s",
        force=True,
    )
    logging.getLogger(logger_name).setLevel(_level_to_int(upper_level))


def _level_to_int(level: str) -> int:
    """把 level 名称转为 stdlib ``logging`` 整数。"""
    return getattr(logging, level)


def get_logger(name: str | None = None, **initial_values: Any) -> Logger:
    """返回绑定 ``name`` 的结构化 logger。

    参数：
        name：logger 名称，通常是模块 ``__name__``。为 ``None`` 时不绑定。
        initial_values：初始上下文字段，等价于 ``logger.bind(**values)``。

    返回的 ``BoundLogger`` 在每次日志输出时会自动合并
    ``CorrelationContext`` 快照，因此调用方无需手动 ``bind(trace_id=...)``。
    """
    logger = structlog.get_logger(name) if name else structlog.get_logger()
    if initial_values:
        logger = logger.bind(**initial_values)
    return logger  # type: ignore[no-any-return]


__all__ = [
    "Logger",
    "configure_logging",
    "get_logger",
    "make_processor_chain",
]
