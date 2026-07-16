"""Workflow Definition、Version 和 Graph 持久化接口。"""

from typing import Protocol
from .schemas import *


class WorkflowRepository(Protocol):
    async def get_workflow(self, workflow_id: str) -> WorkflowView | None:
        """读取稳定 Definition；不存在为 None。"""
        ...
    async def get_version(self, version_id: str) -> WorkflowVersionView | None:
        """读取精确版本状态与 hash；不加载 nodes/edges。"""
        ...
    async def load_graph(self, version_id: str) -> WorkflowGraph | None:
        """一次性加载该版本 nodes/edges 并按 key/priority 稳定排序。"""
        ...
    async def save_workflow(self, item: WorkflowView) -> WorkflowView:
        """按组织唯一 key 保存 Definition。"""
        ...
    async def save_version(self, item: WorkflowVersionView, expected_version: int | None = None) -> WorkflowVersionView:
        """创建 DRAFT 或乐观锁发布；PUBLISHED 禁止修改。"""
        ...
    async def replace_graph(self, version_id: str, graph: WorkflowGraph) -> None:
        """在单事务中替换 DRAFT 的 nodes/edges；不得作用于已发布版本。"""
        ...
