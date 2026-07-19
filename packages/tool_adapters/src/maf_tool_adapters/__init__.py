"""Tool Adapter 公共接口包。

TASK-048 扩展：
- ``base`` 模块定义 ``ToolAdapter`` Protocol 与 ``ToolMetadata`` dataclass。
- ``echo`` 模块提供 ``EchoToolAdapter`` 测试用实现。
"""

from .base import ToolAdapter, ToolMetadata
from .echo import EchoToolAdapter

__all__ = ["EchoToolAdapter", "ToolAdapter", "ToolMetadata"]
