"""Run、Task、Attempt 和事件投影持久化接口。"""

from typing import Protocol
from .schemas import *


class RunRepository(Protocol):
    async def get_run(self, run_id: str) -> RunView | None:
        """读取 Run 当前业务投影；不存在为 None。"""
        ...
    async def save_run(self, run: RunView, expected_version: int | None) -> RunView:
        """按合法状态转换和 expected_version 保存，返回新版本。"""
        ...
    async def get_graph_projection(self, run_id: str) -> RunGraphView | None:
        """读取给 UI 的 graph projection，不访问 checkpoint 数据库。"""
        ...
    async def list_tasks(self, run_id: str, query: TaskQuery) -> TaskPage:
        """按 run/status/node 分页，附带有限 Attempt 摘要。"""
        ...
    async def get_task(self, task_id: str) -> TaskView | None:
        """返回 Task 及 Attempt 历史；不存在为 None。"""
        ...
    async def save_task(self, task: TaskView, expected_version: int | None) -> TaskView:
        """保存合法 Task 状态转换；已终态不能回到 RUNNING。"""
        ...
    async def append_attempt(self, task_id: str, attempt: AttemptView) -> AttemptView:
        """原子分配递增 attempt_no，并保留全部历史 Attempt。"""
        ...
    async def read_events_after(self, run_id: str, event_id: str | None, limit: int) -> list[RunEventView]:
        """按持久顺序读取 event_id 之后的有限事件，供 SSE 回放。"""
        ...
