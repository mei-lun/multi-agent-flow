"""Workflow Definition、Version 和 Graph 持久化接口及内存实现。"""

import asyncio
from copy import deepcopy
from typing import Protocol

from maf_domain.errors import AlreadyExistsError, NotFoundError, VersionConflictError
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


class InMemoryWorkflowRepository:
    """并发安全的具体仓储，适合单进程部署、测试和 SQLite 投影重建前暂存。

    所有返回值均深拷贝，确保从旧版本复制出的 graph 后续完全独立。
    """

    def __init__(self) -> None:
        self.workflows: dict[str, WorkflowView] = {}
        self.versions: dict[str, WorkflowVersionView] = {}
        self.graphs: dict[str, WorkflowGraph] = {}
        self._keys: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def get_workflow(self, workflow_id: str) -> WorkflowView | None:
        item = self.workflows.get(workflow_id)
        return deepcopy(item) if item else None

    async def get_version(self, version_id: str) -> WorkflowVersionView | None:
        item = self.versions.get(version_id)
        return deepcopy(item) if item else None

    async def load_graph(self, version_id: str) -> WorkflowGraph | None:
        graph = self.graphs.get(version_id)
        if graph is None:
            return None
        result = deepcopy(graph)
        result["nodes"] = sorted(result.get("nodes", []), key=lambda item: item["key"])
        result["edges"] = sorted(
            result.get("edges", []), key=lambda item: (item.get("priority", 0), item["key"])
        )
        return result

    async def save_workflow(self, item: WorkflowView) -> WorkflowView:
        async with self._lock:
            owner = self._keys.get(item["key"])
            if owner is not None and owner != item["id"]:
                raise AlreadyExistsError(f"workflow key already exists: {item['key']}")
            self.workflows[item["id"]] = deepcopy(item)
            self._keys[item["key"]] = item["id"]
        return deepcopy(item)

    async def save_version(
        self, item: WorkflowVersionView, expected_version: int | None = None
    ) -> WorkflowVersionView:
        async with self._lock:
            current = self.versions.get(item["id"])
            if current is not None:
                if current["status"] == "PUBLISHED" and item != current:
                    raise VersionConflictError("published workflow version is immutable")
                revision = int(current.get("revision", 1))
                if expected_version is not None and revision != expected_version:
                    raise VersionConflictError(
                        "workflow version revision conflict",
                        context={"expected_version": expected_version, "actual_version": revision},
                    )
                saved = deepcopy(item)
                saved["revision"] = revision + 1
            else:
                if expected_version is not None:
                    raise VersionConflictError("cannot update a missing workflow version")
                saved = deepcopy(item)
                saved.setdefault("revision", 1)
            self.versions[item["id"]] = saved
        return deepcopy(saved)

    async def replace_graph(self, version_id: str, graph: WorkflowGraph) -> None:
        async with self._lock:
            version = self.versions.get(version_id)
            if version is None:
                raise NotFoundError(f"workflow version not found: {version_id}")
            if version["status"] != "DRAFT":
                raise VersionConflictError("only DRAFT workflow versions can be edited")
            self.graphs[version_id] = deepcopy(graph)

    async def replace_graph_with_version(
        self, version_id: str, graph: WorkflowGraph, updated: WorkflowVersionView,
        expected_version: int,
    ) -> WorkflowVersionView:
        """Atomically replace a draft graph and bump its revision."""
        async with self._lock:
            current = self.versions.get(version_id)
            if current is None:
                raise NotFoundError(f"workflow version not found: {version_id}")
            if current["status"] != "DRAFT":
                raise VersionConflictError("only DRAFT workflow versions can be edited")
            if int(current.get("revision", 1)) != expected_version:
                raise VersionConflictError("workflow graph revision conflict")
            self.graphs[version_id] = deepcopy(graph)
            saved = deepcopy(updated)
            saved["revision"] = expected_version + 1
            self.versions[version_id] = saved
            return deepcopy(saved)

    async def next_version_number(self, workflow_id: str) -> int:
        return 1 + max(
            (item["version"] for item in self.versions.values() if item["workflow_id"] == workflow_id),
            default=0,
        )

    async def set_latest_published(self, workflow_id: str, version_id: str) -> None:
        async with self._lock:
            item = self.workflows.get(workflow_id)
            if item is None:
                raise NotFoundError(f"workflow not found: {workflow_id}")
            updated = deepcopy(item)
            updated["latest_published_version_id"] = version_id
            updated["version"] = int(updated["version"]) + 1
            self.workflows[workflow_id] = updated


__all__ = ["WorkflowRepository", "InMemoryWorkflowRepository"]
