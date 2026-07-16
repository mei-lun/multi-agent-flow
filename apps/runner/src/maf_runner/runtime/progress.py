"""合并日志和进度、避免压垮 Server 的接口。"""

from typing import Protocol


class ProgressReporter(Protocol):
    async def report(self, phase: str, percent: int | None, message: str, usage: dict) -> None:
        """脱敏并限频发送摘要；同 phase 高频更新合并，关键状态立即发送。"""
        ...
    async def flush(self) -> None:
        """作业结束前发送最后摘要；失败不阻止结果提交。"""
        ...

