"""外部开源候选、固定版本、复用决定和来源记录接口。"""

from typing import Protocol
from maf_contracts.common import ExecutionContext


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
        """固定候选 commit/tag/hash，运行安全扫描，记录复用方式和来源；协议仅作备注。"""
        ...
    async def record_selection(self, context: ExecutionContext, candidate_id: str, decision: dict) -> str:
        """保存采纳/拒绝理由及来源，返回任务分支内 ExternalReuseManifest 路径。"""
        ...
