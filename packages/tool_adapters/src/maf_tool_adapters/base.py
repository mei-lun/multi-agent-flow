"""Native、HTTP、MCP 实现共同遵守的接口。"""

from typing import Any, Protocol


class ToolAdapter(Protocol):
    adapter_type: str
    async def invoke(self, definition: dict[str, Any], arguments: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        """输入已经授权/约束的定义和参数，输出可序列化原始结果；不得自行扩大权限。"""
        ...
    async def cancel(self, external_call_id: str) -> None:
        """尽力取消；不支持时安全关闭底层连接。"""
        ...

