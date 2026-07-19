"""Native、HTTP、MCP 实现共同遵守的接口。

TASK-048 扩展：
- 新增 ``ToolMetadata`` dataclass，统一描述 Tool 元数据（name、version、
  description、input_schema、output_schema、capabilities、adapter_type）。
- ``ToolAdapter`` Protocol 在原有 ``invoke``/``cancel`` 基础上新增
  ``metadata`` 访问器，要求 Adapter 实现返回 ``ToolMetadata``。
- 本任务范围只声明 ``invoke`` 签名（不实现），注册过程绝不调用 Tool。

设计依据：
- 《多 Agent 协同工具系统设计文档》§11.3 ToolAdapter、§11.4 ToolRegistry。
- TASK-048 验收：注册过程不执行 Tool；Schema 无效或 Native 未在白名单时拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# --------------------------------------------------------------------------- #
# Tool 元数据
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolMetadata:
    """Tool 元数据快照，注册时由 ``ToolAdapter.metadata`` 提供并持久化。

    设计决策：
    - ``name`` + ``version`` 是 Tool 注册表的唯一键（``UNIQUE(name, version)``）；
    - ``version`` 是语义版本字符串（如 ``"1.0.0"``），由 Adapter 自行声明，
      不强制递增；``version_no`` 由 Registry 在持久化时按 name 内部自增，
      用于乐观锁和顺序索引；
    - ``input_schema`` / ``output_schema`` 是 JSON Schema dict，注册时校验
      ``json_valid``；Adapter 必须保证其可序列化；
    - ``capabilities`` 是字符串列表（如 ``["echo", "side_effect_free"]``），
      供 CapabilityPolicy 后续判定（TASK-050 范围）；
    - ``adapter_type`` 取值 ``NATIVE`` / ``HTTP`` / ``MCP``，与
      ``RegisterToolRequest.adapter_type`` 对齐。
    """

    name: str
    version: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    adapter_type: str = "NATIVE"


# --------------------------------------------------------------------------- #
# ToolAdapter Protocol
# --------------------------------------------------------------------------- #


@runtime_checkable
class ToolAdapter(Protocol):
    """Native/HTTP/MCP Tool Adapter 公共接口。

    实现者必须：
    - 通过 ``adapter_type`` 类属性声明 Adapter 类型（``NATIVE`` / ``HTTP`` / ``MCP``）；
    - 通过 ``metadata`` 属性返回完整 ``ToolMetadata``，供 Registry 写入；
    - 提供 ``invoke`` 与 ``cancel`` 方法（本任务不实现，仅签名）。

    安全约束：
    - Registry 在注册流程中 **绝不** 调用 ``invoke`` / ``cancel``；
    - ``invoke`` 仅由 Tool Gateway（TASK-051）在授权通过后调用；
    - ``metadata.input_schema`` / ``output_schema`` 必须可 JSON 序列化。
    """

    adapter_type: str

    @property
    def metadata(self) -> ToolMetadata:
        """返回 Tool 元数据快照；Registry 注册时读取一次并持久化。"""
        ...

    async def invoke(
        self,
        definition: dict[str, Any],
        arguments: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """输入已经授权/约束的定义和参数，输出可序列化原始结果。

        注意：本任务（TASK-048）只声明签名，不实现；Registry 注册过程
        不会调用本方法。实际调用由 Tool Gateway 在 TASK-051 中实现。
        """
        ...

    async def cancel(self, external_call_id: str) -> None:
        """尽力取消；不支持时安全关闭底层连接。

        本任务只声明签名，不实现。
        """
        ...


__all__ = ["ToolAdapter", "ToolMetadata"]
