"""Tool 定义与 MCP 映射持久化接口。"""

from typing import Protocol
from .schemas import ToolView


class ToolRepository(Protocol):
    async def get_by_key(self, key: str, version: int | None = None) -> ToolView | None:
        """按稳定 key 和可选精确 version 查询；运行时必须传 version。"""
        ...
    async def save(self, tool: ToolView) -> ToolView:
        """保存新版本或状态；Schema/adapter/risk 变化必须生成版本。"""
        ...
    async def list_by_mcp_server(self, mcp_server_id: str) -> list[ToolView]:
        """返回该 Server 同步产生的当前 Tool，用于差异计算。"""
        ...
    async def disable_missing_mcp_tools(self, mcp_server_id: str, present_keys: set[str]) -> list[str]:
        """把远端缺失的当前版本标 DISABLED，返回受影响 ID；保留历史。"""
        ...
