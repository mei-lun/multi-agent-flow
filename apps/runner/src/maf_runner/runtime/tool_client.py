"""在节点本地请求获授权 Tool 的接口。"""

from typing import Protocol
from maf_contracts.tool import *


class ToolClient(Protocol):
    async def list_allowed(self) -> ToolListResponse:
        """从 Role/Task 快照和本地 Tool Registry 求交集；未授权工具不暴露。"""
        ...
    async def call(self, tool_key: str, request: ToolCallRequest) -> ToolCallResult:
        """经本地 Policy 校验后执行；需中央人工审批的动作先报告 BLOCKED，不跨节点 HTTP 等待。"""
        ...
    async def cancel(self, call_id: str, reason: str) -> None:
        """请求取消同一 Attempt 的调用；调用不存在或已结束时按幂等成功处理。"""
        ...
