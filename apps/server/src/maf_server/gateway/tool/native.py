"""小型进程内白名单 Native Tool 接口。"""

from typing import Any, Protocol


class NativeTool(Protocol):
    key: str
    async def invoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """只处理已由 Gateway 校验/约束的参数；返回可序列化结果。"""
        ...


class NativeToolRegistry(Protocol):
    def get(self, key: str, version: int) -> NativeTool | None:
        """只返回启动时显式注册的实现，禁止动态 import 用户路径。"""
        ...
