"""模型 Provider Adapter 公共接口包。

TASK-038 范围：
- ``base`` 模块定义 ``ProviderAdapter`` Protocol（供应商无关）；
- ``mock_adapter`` 提供测试用 ``MockProviderAdapter``（无网络）；
- ``openai_compatible_adapter`` 提供 ``OpenAICompatibleAdapter``（OpenAI/GLM/DeepSeek/MiniMax）；
- ``anthropic_adapter`` 提供 ``AnthropicProviderAdapter``（Anthropic Claude）。

设计依据：
- 《多 Agent 协同工具系统设计文档》§11.3 ProviderAdapter、§2.4 LiteLLM 边界。
- 凭据从 ``connection_config["api_key"]`` 注入，Adapter 不直接访问 SecretService。
"""

from .anthropic_adapter import AnthropicProviderAdapter
from .base import ProviderAdapter
from .mock_adapter import MockProviderAdapter
from .openai_compatible_adapter import OpenAICompatibleAdapter

__all__ = [
    "AnthropicProviderAdapter",
    "MockProviderAdapter",
    "OpenAICompatibleAdapter",
    "ProviderAdapter",
]
