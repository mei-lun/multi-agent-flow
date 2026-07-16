"""MCP client 生命周期、能力发现、调用与归一化接口。"""

from typing import Any, Protocol


class McpClient(Protocol):
    async def list_tools(self, server_id: str) -> list[dict[str, Any]]:
        """建立受控会话并返回远端工具 Schema；不执行工具。"""
        ...
    async def call_tool(self, server_id: str, remote_key: str, arguments: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        """执行一次 MCP 调用；限制超时/输出并把协议错误归一化。"""
        ...
    async def cancel(self, call_id: str) -> None:
        """协议支持时取消；不支持时关闭会话并标记尽力取消。"""
        ...
