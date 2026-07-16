"""由内嵌 LiteLLM SDK 实现的供应商 Adapter 接口。"""

from typing import Any, AsyncIterator, Protocol

from maf_contracts.model import UnifiedModelRequest, UnifiedModelResponse


class ModelAdapter(Protocol):
    adapter_type: str

    async def probe(self, resolved_connection: dict[str, Any]) -> dict[str, Any]:
        """用已解析但不落盘的凭据测试连接，返回归一化检查结果。"""
        ...

    async def list_models(self, resolved_connection: dict[str, Any]) -> list[dict[str, Any]]:
        """列出远端声明模型；供应商不支持时返回空列表而非猜测。"""
        ...

    async def invoke(
        self, resolved_connection: dict[str, Any], model_name: str, request: UnifiedModelRequest
    ) -> UnifiedModelResponse:
        """执行一次非流式调用并把供应商字段归一化；不得返回密钥或原始异常。"""
        ...

    async def stream(
        self, resolved_connection: dict[str, Any], model_name: str, request: UnifiedModelRequest
    ) -> AsyncIterator[dict[str, Any]]:
        """产生规范化增量；调用取消时必须关闭底层连接。"""
        ...

    def normalize_error(self, error: Exception) -> dict[str, Any]:
        """映射为稳定 code/retryable/category，并移除请求头、URL 凭据和响应敏感内容。"""
        ...
