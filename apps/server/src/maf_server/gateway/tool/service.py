"""Authorize and route tool calls to native or MCP implementations."""


class ToolGateway:
    def invoke(self, call_id: str) -> str:
        raise NotImplementedError

