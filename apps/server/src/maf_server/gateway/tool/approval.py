"""高风险 Tool 调用的挂起和审批恢复接口。"""

from typing import Protocol


class ToolApprovalService(Protocol):
    async def request(self, tool_call_id: str, subject_version: str, summary: str) -> str:
        """创建站内待办并返回 inbox_item_id；不得在审批前调用 Tool。"""
        ...
    async def resume(self, decision_id: str) -> None:
        """再次校验 subject version、权限和参数后执行或拒绝原 ToolCall。"""
        ...
