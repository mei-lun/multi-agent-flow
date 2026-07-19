"""测试用 EchoToolAdapter，回显参数，不执行真实副作用。

TASK-048 范围：
- 提供一个最小可用的 ``ToolAdapter`` 实现，供 ToolRegistryService 注册流程
  与契约测试使用；
- ``invoke`` 在本任务中只声明为简单回显实现，**Registry 注册过程不会调用它**
  （TASK-048 禁止调用 Tool）；
- ``EchoToolAdapter`` 的 ``adapter_type`` 为 ``NATIVE``，metadata 提供
  基础 JSON Schema，便于测试 Schema 校验路径。
"""

from __future__ import annotations

from typing import Any

from .base import ToolMetadata


class EchoToolAdapter:
    """测试用 Native Adapter：注册时返回固定 metadata，invoke 回显参数。

    本类仅用于：
    - ToolRegistryService 单元/契约测试；
    - 文档示例；
    - 验证 ``ToolAdapter`` Protocol 兼容性。

    生产 Native Tool Adapter 应在 ``gateway/tool/native.py``（TASK-051 范围）
    实现；本类不进入生产路径。
    """

    adapter_type: str = "NATIVE"

    def __init__(
        self,
        *,
        name: str = "echo",
        version: str = "1.0.0",
        description: str = "Echoes back the input arguments (test only).",
        capabilities: list[str] | None = None,
    ) -> None:
        self._metadata: ToolMetadata = ToolMetadata(
            name=name,
            version=version,
            description=description,
            input_schema={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                },
            },
            capabilities=list(capabilities) if capabilities else ["echo"],
            adapter_type=self.adapter_type,
        )

    @property
    def metadata(self) -> ToolMetadata:
        """返回 Tool 元数据快照。"""
        return self._metadata

    async def invoke(
        self,
        definition: dict[str, Any],
        arguments: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """回显 arguments；本任务不调用，仅签名实现。

        TASK-048 验收明确：注册过程不执行 Tool。本方法仅在 Tool Gateway
        （TASK-051）实际调用 Tool 时被触发，目前仅作为 Protocol 兼容实现。
        """
        return {"message": arguments.get("message", ""), "echoed": True}

    async def cancel(self, external_call_id: str) -> None:
        """Echo 无副作用，cancel 是 no-op。"""
        return None


__all__ = ["EchoToolAdapter"]
