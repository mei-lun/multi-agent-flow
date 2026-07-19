"""由内嵌 LiteLLM SDK 实现的供应商 Adapter 接口与工厂。

TASK-038 范围：
- 保留原 ``ModelAdapter`` Protocol 作为 ``ProviderAdapter`` 的别名（向后兼容，
  ``doc/接口总目录.md`` 仍引用 ``ModelAdapter``）；
- 新增 ``ProviderAdapterFactory``，按 provider 名称创建 Adapter，支持注册自定义工厂；
- 凭据从 ``connection_config["api_key"]`` 注入，工厂不访问 ``SecretService``。

设计依据：
- 《多 Agent 协同工具系统设计文档》§11.3 ProviderAdapter、§2.4 LiteLLM 边界。
- TASK-038 验收：Codex/OpenAI兼容/GLM/DeepSeek/MiniMax/Kimi Code 可按配置路由；
  Provider 错误统一为 code/category/retryable；异常和日志不含 Key。

安全约束：
- ``connection_config`` 中的 ``api_key`` 仅透传给 Adapter，工厂不记录、不持久化；
- 未知 provider 抛 ``UnsupportedOperationError``，不返回默认 Adapter 以避免误用。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Protocol, runtime_checkable

from maf_contracts.model import UnifiedModelRequest, UnifiedModelResponse
from maf_domain.errors import UnsupportedOperationError

from maf_provider_adapters import (
    AnthropicProviderAdapter,
    MockProviderAdapter,
    OpenAICompatibleAdapter,
    ProviderAdapter,
)


# --------------------------------------------------------------------------- #
# 向后兼容：ModelAdapter 别名
# --------------------------------------------------------------------------- #


@runtime_checkable
class ModelAdapter(Protocol):
    """供应商 Adapter 协议（``ProviderAdapter`` 的向后兼容别名）。

    与 ``packages/provider_adapters/src/maf_provider_adapters/base.py::ProviderAdapter``
    结构一致；保留此类是为了 ``doc/接口总目录.md`` 引用的稳定性。
    新代码应直接使用 ``ProviderAdapter``。
    """

    adapter_type: str

    async def probe(self, resolved_connection: dict[str, Any]) -> dict[str, Any]:
        """用已解析但不落盘的凭据测试连接，返回归一化检查结果。"""
        ...

    async def list_models(
        self, resolved_connection: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """列出远端声明模型；供应商不支持时返回空列表而非猜测。"""
        ...

    async def invoke(
        self,
        resolved_connection: dict[str, Any],
        model_name: str,
        request: UnifiedModelRequest,
    ) -> UnifiedModelResponse:
        """执行一次非流式调用并把供应商字段归一化；不得返回密钥或原始异常。"""
        ...

    def stream(
        self,
        resolved_connection: dict[str, Any],
        model_name: str,
        request: UnifiedModelRequest,
    ) -> AsyncIterator[dict[str, Any]]:
        """产生规范化增量；调用取消时必须关闭底层连接。"""
        ...

    def normalize_error(self, error: Exception) -> dict[str, Any]:
        """映射为稳定 code/retryable/category，并移除请求头、URL 凭据和响应敏感内容。"""
        ...


# --------------------------------------------------------------------------- #
# ProviderAdapterFactory
# --------------------------------------------------------------------------- #


#: Adapter 工厂函数类型：接收 connection_config，返回 ProviderAdapter 实例。
AdapterFactoryFn = Callable[[dict[str, Any]], ProviderAdapter]


class ProviderAdapterFactory:
    """按 provider 名称创建 ``ProviderAdapter`` 的工厂。

    使用方式：
    ::

        factory = ProviderAdapterFactory()
        adapter = factory.create_adapter("openai", {
            "api_key": "...",          # 由 SecretService 解析后注入
            "api_base": "https://api.openai.com/v1",
        })
        resp = await adapter.invoke(connection, "gpt-4o", request)

    凭据注入：
    - ``connection_config["api_key"]`` 由 ``ModelConnectionService`` 经
      ``SecretService.resolve`` 解析后传入；工厂与 Adapter 均不访问 SecretService；
    - 工厂不记录 ``api_key``，不将其写入日志或返回值。

    注册自定义 Adapter：
    ::

        factory.register_adapter("my_provider", lambda cfg: MyAdapter(cfg))

    线程安全：
    - ``register_adapter`` 修改内部注册表，应在应用启动阶段完成注册；
    - 运行时 ``create_adapter`` 只读注册表。
    """

    def __init__(self) -> None:
        self._registry: dict[str, AdapterFactoryFn] = dict(self._default_registry())

    # ------------------------------------------------------------------ #
    # 公开方法
    # ------------------------------------------------------------------ #

    def create_adapter(
        self, provider: str, connection_config: dict[str, Any]
    ) -> ProviderAdapter:
        """按 provider 名称创建 Adapter 实例。

        :param provider: provider 标识，与 ``model_connections.provider`` 对齐
            （``openai``/``anthropic``/``azure``/``local``/``mock``）；
        :param connection_config: 已解析的连接配置，至少包含 ``api_key`` 与
            ``api_base``；凭据由调用方经 SecretService 解析后注入。
        :raises UnsupportedOperationError: provider 未注册。
        """
        if not isinstance(provider, str) or not provider:
            raise UnsupportedOperationError(
                "provider 不能为空",
                context={"provider": provider},
            )
        factory = self._registry.get(provider)
        if factory is None:
            raise UnsupportedOperationError(
                f"未注册的 provider：{provider}",
                context={
                    "provider": provider,
                    "registered": sorted(self._registry.keys()),
                },
            )
        config = dict(connection_config) if isinstance(connection_config, dict) else {}
        return factory(config)

    def register_adapter(self, provider: str, factory: AdapterFactoryFn) -> None:
        """注册自定义 Adapter 工厂。

        :param provider: provider 标识（重复注册覆盖旧工厂）；
        :param factory: 工厂函数，接收 ``connection_config`` 返回 ``ProviderAdapter``。
        """
        if not isinstance(provider, str) or not provider:
            raise UnsupportedOperationError("provider 不能为空")
        if not callable(factory):
            raise UnsupportedOperationError("factory 必须可调用")
        self._registry[provider] = factory

    def list_registered(self) -> list[str]:
        """返回已注册 provider 名称（按字母序）。"""
        return sorted(self._registry.keys())

    def is_registered(self, provider: str) -> bool:
        """检查 provider 是否已注册。"""
        return provider in self._registry

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _default_registry() -> dict[str, AdapterFactoryFn]:
        """默认 provider → factory 映射。"""

        def _openai_factory(cfg: dict[str, Any]) -> ProviderAdapter:
            return OpenAICompatibleAdapter(connection_config=cfg)

        def _openai_compatible_factory(cfg: dict[str, Any]) -> ProviderAdapter:
            return OpenAICompatibleAdapter(connection_config=cfg)

        def _anthropic_factory(cfg: dict[str, Any]) -> ProviderAdapter:
            return AnthropicProviderAdapter(connection_config=cfg)

        def _azure_factory(cfg: dict[str, Any]) -> ProviderAdapter:
            # Azure OpenAI 使用 OpenAICompatibleAdapter，api_base 指向 Azure 端点；
            # Azure 凭据可能是 api_key 或 azure_ad_token，由 connection_config 传入。
            return OpenAICompatibleAdapter(connection_config=cfg)

        def _local_factory(cfg: dict[str, Any]) -> ProviderAdapter:
            # 本地推理端点（如 Ollama）通常兼容 OpenAI 协议且无需 api_key。
            return OpenAICompatibleAdapter(connection_config=cfg)

        def _mock_factory(cfg: dict[str, Any]) -> ProviderAdapter:
            return MockProviderAdapter(connection_config=cfg)

        return {
            "openai": _openai_factory,
            "openai_compatible": _openai_compatible_factory,
            "anthropic": _anthropic_factory,
            "azure": _azure_factory,
            "local": _local_factory,
            "mock": _mock_factory,
        }


#: 模块级默认工厂实例，供单进程内复用。
_default_factory: ProviderAdapterFactory | None = None


def get_default_factory() -> ProviderAdapterFactory:
    """返回模块级默认工厂实例（惰性创建，单例）。"""
    global _default_factory
    if _default_factory is None:
        _default_factory = ProviderAdapterFactory()
    return _default_factory


__all__ = [
    "AdapterFactoryFn",
    "ModelAdapter",
    "ProviderAdapterFactory",
    "get_default_factory",
]
