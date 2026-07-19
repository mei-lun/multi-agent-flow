"""MCP client 生命周期、能力发现、调用与归一化接口。

TASK-049 扩展：
- 新增 ``McpToolInfo`` dataclass：MCP 服务器暴露的远端工具元数据快照
  （name、description、input_schema、output_schema）；
- 新增 ``McpClient`` 具体类：连接 MCP 服务器、列出工具、断开连接
  （``connect`` / ``list_tools`` / ``disconnect``）；
- 本任务范围只做**发现**，不执行 ``tools/call``（TASK-051 范围）。

实现说明：
- ``McpClient`` 是 **Mock 实现**，不依赖真实 MCP 服务器或 MCP Python SDK：
  构造时接受 ``tools_registry``（``{url: [McpToolInfo, ...]}``），供测试与
  开发期注入预置工具列表；未配置的 url 返回空列表。
- ``connect`` 校验 url 非空并缓存 ``credentials``（凭据对象，**不写日志、不
  持久化明文**）；``list_tools`` 必须在已连接后调用，否则抛 ``RuntimeError``；
  ``disconnect`` 释放会话状态（幂等）。
- 凭据安全：``credentials`` 由调用方（``McpToolSyncService``）从
  ``SecretService.resolve`` 取得后传入；本类不解析、不存储、不日志明文。
- 协议错误归一化：未来接入真实 SDK 时，``list_tools`` 应把 JSON-RPC 错误
  归一化为 ``ExternalDependencyError``；当前 Mock 仅在未连接时抛本地错误。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from maf_domain.errors import ArgumentError

# --------------------------------------------------------------------------- #
# McpToolInfo：远端工具元数据快照
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class McpToolInfo:
    """MCP 服务器 ``tools/list`` 返回的单条工具元数据。

    字段语义对齐 MCP 规范 ``tools/list`` 响应：
    - ``name``：工具业务名（非空字符串），与 ``ToolMetadata.name`` 对齐；
    - ``description``：人类可读描述，可为空串；
    - ``input_schema``：输入 JSON Schema dict（``tools/list`` 的
      ``inputSchema``），注册时写入 ``tools.input_schema``；
    - ``output_schema``：输出 JSON Schema dict；MCP 规范未强制声明输出
      schema，缺失时为空 dict，由同步服务回退为 ``{"type": "object"}`` 占位
      以满足 ``tools`` 表 ``json_valid`` 约束。
    """

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# McpClientLike：发现协议（供 McpToolSyncService 依赖注入）
# --------------------------------------------------------------------------- #


class McpClientLike(Protocol):
    """MCP 工具发现协议；``McpToolSyncService`` 依赖此协议而非具体类。

    任何实现 ``connect`` / ``list_tools`` / ``disconnect`` 的对象均满足本协议；
    生产实现将包装 MCP Python SDK（未来任务），测试可注入 Mock。
    """

    async def connect(
        self, url: str, *, credentials: Mapping[str, Any] | None = None
    ) -> None:
        """连接 MCP 服务器；``credentials`` 由 SecretService 解析后传入。"""
        ...

    async def list_tools(self) -> list[McpToolInfo]:
        """列出 MCP 服务器暴露的工具；必须在 ``connect`` 之后调用。"""
        ...

    async def disconnect(self) -> None:
        """断开会话；幂等。"""
        ...


# --------------------------------------------------------------------------- #
# McpClient：Mock 实现（不依赖真实 MCP 服务器）
# --------------------------------------------------------------------------- #


class McpClient:
    """MCP 客户端 Mock 实现：发现远端工具，不执行调用。

    本类满足 ``McpClientLike`` Protocol。构造时注入 ``tools_registry``
    可为不同 url 配置预置工具列表；``connect`` 缓存 url 与凭据，
    ``list_tools`` 按 url 返回对应工具。

    设计决策：
    - **不连接真实服务器**：避免测试依赖网络与外部进程；真实 SDK 接入
      属于后续任务；
    - **凭据不持久化**：``credentials`` 仅在内存会话期内可见，``disconnect``
      后立即清空；不写日志、不写数据库明文；
    - **未连接即 list_tools 抛错**：强制调用方先 ``connect``，避免误用；
    - **disconnect 幂等**：重复调用安全，无副作用。
    """

    def __init__(
        self,
        tools_registry: Mapping[str, Sequence[McpToolInfo]] | None = None,
    ) -> None:
        self._tools_registry: dict[str, list[McpToolInfo]] = {
            url: list(tools) for url, tools in (tools_registry or {}).items()
        }
        self._url: str | None = None
        self._credentials: Mapping[str, Any] | None = None
        self._connected: bool = False

    @property
    def connected(self) -> bool:
        """是否处于已连接状态。"""
        return self._connected

    @property
    def url(self) -> str | None:
        """当前连接的 MCP 服务器 url；未连接返回 None。"""
        return self._url

    async def connect(
        self,
        url: str,
        *,
        credentials: Mapping[str, Any] | None = None,
    ) -> None:
        """连接 MCP 服务器；缓存 url 与凭据（明文不写日志）。

        :param url: MCP 服务器 endpoint，非空字符串。
        :param credentials: 凭据键值（由 SecretService.resolve 解析），
            供未来 SDK 鉴权使用；本 Mock 不读取其内容。
        :raises ArgumentError: ``url`` 非字符串或为空。
        """
        if not isinstance(url, str) or not url:
            raise ArgumentError(
                "MCP server url 不能为空",
                context={"field": "url"},
            )
        self._url = url
        self._credentials = credentials
        self._connected = True

    async def list_tools(self) -> list[McpToolInfo]:
        """列出已连接 MCP 服务器的工具；返回 ``McpToolInfo`` 列表。

        :raises RuntimeError: 未调用 ``connect`` 即调用本方法。
        :returns: ``McpToolInfo`` 列表副本；未配置的 url 返回空列表。
        """
        if not self._connected or self._url is None:
            raise RuntimeError("MCP client 未连接：请先调用 connect()")
        return list(self._tools_registry.get(self._url, []))

    async def disconnect(self) -> None:
        """断开会话；幂等，清空 url 与凭据。"""
        self._connected = False
        self._url = None
        self._credentials = None


__all__ = [
    "McpClient",
    "McpClientLike",
    "McpToolInfo",
]
