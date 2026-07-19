"""虚拟时钟：实现 ``maf_server.core.clock.Clock`` Protocol。

设计目标：
- 让审批、lease 超时和调度测试可确定复现，不依赖系统时钟；
- ``now()`` 返回当前虚拟时间（UTC）；
- ``advance()`` 手动推进虚拟时间；
- ``wait_until()`` 立即推进到 deadline，测试不真实阻塞。

与设计文档 §27.4「使用可控时钟测试审批和 lease 超时」对齐。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Final

_UTC: Final[timezone] = timezone.utc


class VirtualClock:
    """可手动推进的虚拟时钟，满足 ``Clock`` Protocol（``now``/``wait_until``）。"""

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            self._now = datetime(2024, 1, 1, 0, 0, 0, tzinfo=_UTC)
        else:
            if start.tzinfo is None:
                raise ValueError("VirtualClock 要求带时区的 start（用 UTC）")
            self._now = start

    def now(self) -> datetime:
        """返回当前虚拟时间（带 UTC 时区）。"""
        return self._now

    def advance(self, delta: timedelta | float) -> datetime:
        """推进虚拟时间并返回推进后的时间。

        ``delta`` 可为 ``timedelta`` 或秒数（``float``/``int``）。
        """
        if isinstance(delta, timedelta):
            self._now = self._now + delta
        else:
            self._now = self._now + timedelta(seconds=delta)
        return self._now

    async def wait_until(self, deadline: datetime) -> None:
        """立即推进到 deadline（若 deadline 在未来），不真实等待。

        与 ``Clock`` Protocol 语义一致：测试实现可以立即推进虚拟时间，
        避免 ``asyncio.sleep`` 真实阻塞。
        """
        if deadline > self._now:
            self._now = deadline


__all__ = ["VirtualClock"]
