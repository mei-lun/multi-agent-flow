"""外部开源候选、固定版本、复用决定和来源记录接口。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from .scanner import ExternalSourceScanner

if TYPE_CHECKING:
    from maf_contracts.common import ExecutionContext
else:
    ExecutionContext = dict[str, Any]


class SearchQuery(dict):
    """查询包含关键词、语言、允许域名、最大结果数和项目数据分类。"""


class SearchProvider(Protocol):
    async def search(self, query: SearchQuery) -> list[dict]:
        """按网络策略检索候选，只返回标题、URL、摘要和来源元数据。"""
        ...
    async def fetch(self, url: str, policy: dict) -> dict:
        """下载允许 URL，限制重定向、大小、类型和超时，返回内容 Artifact 引用。"""
        ...


class ExternalReuseService(Protocol):
    async def discover(self, context: ExecutionContext, query: SearchQuery) -> list[dict]:
        """确认角色具有外网检索 Tool 后搜索、去重并记录候选，不自动写入代码仓库。"""
        ...

    async def evaluate(self, context: ExecutionContext, candidate: dict) -> dict:
        """固定候选 commit/tag/hash，运行安全扫描，记录复用方式。"""
        ...

    async def record_selection(self, context: ExecutionContext, candidate_id: str, decision: dict) -> str:
        """保存采纳/拒绝理由及来源，返回任务分支内 manifest 路径。"""
        ...


class LocalExternalReuseService:
    """Controlled search/fetch/evaluate flow with an append-only reuse manifest."""

    def __init__(self, *, search_provider: SearchProvider, scanner: ExternalSourceScanner) -> None:
        self.search_provider = search_provider
        self.scanner = scanner

    @staticmethod
    def _allowed(context: ExecutionContext, capability: str) -> bool:
        return capability in set(context.get("granted_tool_keys", [])) or "external.search" in set(context.get("granted_tool_keys", []))

    async def discover(self, context: ExecutionContext, query: SearchQuery) -> list[dict]:
        if not self._allowed(context, "external.search"):
            raise PermissionError("external search capability is not granted")
        results = await self.search_provider.search(dict(query))
        # Search results are metadata only; no result is written to the workspace.
        return [{key: value for key, value in item.items() if key not in {"content", "path", "absolute_path"}} for item in results]

    async def evaluate(self, context: ExecutionContext, candidate: dict) -> dict:
        if not self._allowed(context, "external.search"):
            raise PermissionError("external search capability is not granted")
        source = candidate.get("source_artifact_version_id") or candidate.get("path")
        fixed = candidate.get("commit") or candidate.get("version") or candidate.get("sha256")
        if not source or not fixed:
            raise ValueError("candidate must identify a source and fixed commit/version/hash")
        scan = await self.scanner.scan(str(source), candidate.get("ecosystem"))
        return {"candidate_id": candidate.get("candidate_id") or hashlib.sha256(str(source).encode()).hexdigest()[:24],
                "source": str(source), "fixed_version": str(fixed), "scan": scan,
                "accepted": bool(scan.get("safe")), "install_executed": False}

    async def record_selection(self, context: ExecutionContext, candidate_id: str, decision: dict) -> str:
        workspace = Path(str(context.get("workspace_path", "."))).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        target = (workspace / "ExternalReuseManifest.json").resolve()
        try:
            target.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("manifest path escapes workspace") from exc
        manifest = {"schema_version": 1, "candidate_id": candidate_id, "decision": dict(decision),
                    "project_id": context.get("project_id"), "task_id": context.get("task_id"),
                    "assignment_epoch": context.get("assignment_epoch")}
        target.write_text(json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return "ExternalReuseManifest.json"


__all__ = ["ExternalReuseService", "LocalExternalReuseService", "SearchProvider", "SearchQuery"]
