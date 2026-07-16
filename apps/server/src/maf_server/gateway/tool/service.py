"""Tool 授权、审批、执行与结果归一化接口。"""

from typing import Protocol
from maf_contracts.common import ExecutionContext
from maf_contracts.tool import *


class ToolGateway(Protocol):
    async def list_allowed(self, context: ExecutionContext) -> ToolListResponse:
        """返回 Role Snapshot 与 Git task grant 交集中的 Tool，不返回隐藏工具。"""
        ...

    async def call(
        self, context: ExecutionContext, tool_key: str, request: ToolCallRequest
    ) -> ToolCallResult:
        """执行或挂起一个 Tool 调用。

        校验 control/assignment/call_key；确认精确 Tool Version 获授权；按 input schema 校验参数；
        调 PolicyService 获取约束后参数和 obligations；需要审批时保存 WAITING_APPROVAL 与
        Inbox Item 后返回，不执行 Adapter；允许时按类型调用 Native/HTTP/MCP Adapter；限制
        时间和输出大小；校验 output schema；保存 ToolCall、Artifact、审计和事件。
        """
        ...

    async def get_call(self, context: ExecutionContext, call_id: str) -> ToolCallView:
        """读取同一 task/assignment 的节点本地脱敏状态。"""
        ...

    async def cancel_call(
        self, context: ExecutionContext, call_id: str, request: CancelToolCallRequest
    ) -> ToolCallView:
        """幂等取消进行中或待审批调用，并通知 Adapter 尽力停止。"""
        ...
