"""可替换时钟接口，使租约、超时和事件测试可确定复现。"""

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """返回带 UTC 时区的时间；业务代码禁止直接调用 datetime.now。"""
        ...
    async def wait_until(self, deadline: datetime) -> None:
        """等待到 deadline；测试实现可以立即推进虚拟时间。"""
        ...

