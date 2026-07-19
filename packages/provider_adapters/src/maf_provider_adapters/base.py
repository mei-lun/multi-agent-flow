"""供应商无关 Provider Adapter 接口。"""

from typing import Any, AsyncIterator, Protocol
from maf_contracts.model import UnifiedModelRequest, UnifiedModelResponse


class ProviderAdapter(Protocol):
    adapter_type: str
    async def probe(self, connection: dict[str, Any]) -> dict[str, Any]:
        """输入短期 resolved connection，输出脱敏连接检查；不保存凭据。"""
        ...
    async def list_models(self, connection: dict[str, Any]) -> list[dict[str, Any]]:
        """输出远端声明模型；不支持列表时返回空列表。"""
        ...
    async def invoke(self, connection: dict[str, Any], model_name: str, request: UnifiedModelRequest) -> UnifiedModelResponse:
        """执行并归一化一次调用；权限、预算和 fallback 不属于 Adapter。"""
        ...
    def stream(self, connection: dict[str, Any], model_name: str, request: UnifiedModelRequest) -> AsyncIterator[dict[str, Any]]:
        """输出规范化流增量，取消时关闭底层连接。"""
        ...
    def normalize_error(self, error: Exception) -> dict[str, Any]:
        """转换稳定错误码/retryable/category 并移除敏感信息。"""
        ...
