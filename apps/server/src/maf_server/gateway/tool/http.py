"""受控 HTTP Tool Adapter 接口。"""

from typing import Any, Protocol


class HttpToolAdapter(Protocol):
    async def invoke(self, definition: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
        """只访问定义和 Policy 都允许的 URL/method；禁止重定向到私网、限制响应大小和超时。"""
        ...
