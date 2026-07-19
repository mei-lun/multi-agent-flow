"""TASK-038 契约测试：Provider Adapter 基线。

验收标准覆盖：

1. ``ProviderAdapter`` Protocol 定义清晰，含
   ``adapter_type``/``probe``/``list_models``/``invoke``/``stream``/``normalize_error``。
2. 数据模型（``UnifiedModelRequest``/``UnifiedModelResponse``/``ModelUsage``/
   ``CanonicalMessage``）在 ``maf_contracts.model`` 已定义完整。
3. 内置 Adapter：``MockProviderAdapter``/``OpenAICompatibleAdapter``/
   ``AnthropicProviderAdapter``。
4. ``ProviderAdapterFactory`` 按 provider 名称创建 Adapter。
5. 凭据从 ``connection_config["api_key"]`` 注入，Adapter 不直接访问 SecretService。
6. ``MockProviderAdapter`` 无网络调用。
7. Provider 错误统一为 ``code``/``category``/``retryable``；异常和响应不含 Key。
8. 自定义 Adapter 可经 ``register_adapter`` 注册。

测试范围：
- ``packages/provider_adapters/src/maf_provider_adapters/{base,mock_adapter,
  openai_compatible_adapter,anthropic_adapter}.py``；
- ``apps/server/src/maf_server/gateway/model/adapters.py``（Factory）；
- ``packages/contracts_py/src/maf_contracts/model.py``（数据模型契约）。

不测试：实际模型推理调用（无网络/无 Key）、Role 权限、fallback 策略。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, get_type_hints

import httpx
import pytest

from maf_contracts.model import (
    CanonicalMessage,
    ModelUsage,
    UnifiedModelRequest,
    UnifiedModelResponse,
)
from maf_domain.errors import UnsupportedOperationError
from maf_provider_adapters import (
    AnthropicProviderAdapter,
    MockProviderAdapter,
    OpenAICompatibleAdapter,
    ProviderAdapter,
)
from maf_server.gateway.model.adapters import (
    AdapterFactoryFn,
    ModelAdapter,
    ProviderAdapterFactory,
    get_default_factory,
)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _make_request(
    *,
    call_key: str = "test-call-1",
    max_output_tokens: int = 100,
    temperature: float | None = 0.7,
) -> UnifiedModelRequest:
    """构造测试用 UnifiedModelRequest。"""
    return UnifiedModelRequest(
        attempt_id="attempt-1",
        call_key=call_key,
        model_policy_id="policy-1",
        messages=[CanonicalMessage(role="user", content="hello")],
        tools=[],
        response_schema=None,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        timeout_seconds=30,
        metadata={"trace_id": "trace-1"},
    )


def _make_connection(
    *,
    api_key: str = "sk-test-FAKE-KEY-1234567890",
    api_base: str = "https://api.openai.com/v1",
    **extra: Any,
) -> dict[str, Any]:
    """构造测试用 resolved connection，含已解析凭据。"""
    conn: dict[str, Any] = {"api_key": api_key, "api_base": api_base}
    conn.update(extra)
    return conn


_FAKE_API_KEY = "sk-OPENAI-task038-FAKE-SECRET-1234567890"


# --------------------------------------------------------------------------- #
# 验收 1：ProviderAdapter Protocol 定义清晰
# --------------------------------------------------------------------------- #


class TestProviderAdapterProtocol:
    """``ProviderAdapter`` Protocol 结构校验。"""

    def test_protocol_defines_adapter_type(self) -> None:
        """Protocol 必须声明 ``adapter_type`` 属性注解。"""
        hints = get_type_hints(ProviderAdapter)
        assert "adapter_type" in hints, "ProviderAdapter 缺少 adapter_type 注解"

    def test_protocol_defines_required_methods(self) -> None:
        """Protocol 必须声明 5 个方法 + 1 个属性注解。"""
        # adapter_type 是属性注解（Protocol 不创建类属性）
        hints = get_type_hints(ProviderAdapter)
        assert "adapter_type" in hints, "ProviderAdapter 缺少 adapter_type 注解"
        # 方法在 Protocol 上是可访问的
        for name in ("probe", "list_models", "invoke", "stream", "normalize_error"):
            assert hasattr(ProviderAdapter, name), f"ProviderAdapter 缺少方法 {name}"

    def test_model_adapter_alias_is_backward_compatible(self) -> None:
        """``ModelAdapter`` 作为 ``ProviderAdapter`` 的向后兼容别名，结构一致。"""
        hints = get_type_hints(ModelAdapter)
        assert "adapter_type" in hints, "ModelAdapter 缺少 adapter_type 注解"
        for name in ("probe", "list_models", "invoke", "stream", "normalize_error"):
            assert hasattr(ModelAdapter, name), f"ModelAdapter 缺少方法 {name}"

    def test_all_built_in_adapters_satisfy_protocol_structure(self) -> None:
        """内置 Adapter 必须实现 Protocol 的全部方法。"""
        adapters = [
            MockProviderAdapter(),
            OpenAICompatibleAdapter(),
            AnthropicProviderAdapter(),
        ]
        for adapter in adapters:
            for name in (
                "adapter_type",
                "probe",
                "list_models",
                "invoke",
                "stream",
                "normalize_error",
            ):
                assert hasattr(adapter, name), (
                    f"{type(adapter).__name__} 缺少 {name}"
                )

    def test_adapter_type_is_nonempty_string(self) -> None:
        """``adapter_type`` 必须是非空字符串。"""
        for adapter in (
            MockProviderAdapter(),
            OpenAICompatibleAdapter(),
            AnthropicProviderAdapter(),
        ):
            assert isinstance(adapter.adapter_type, str)
            assert adapter.adapter_type


# --------------------------------------------------------------------------- #
# 验收 2：数据模型定义完整
# --------------------------------------------------------------------------- #


class TestDataModels:
    """``UnifiedModelRequest``/``UnifiedModelResponse`` 数据模型契约。"""

    def test_unified_model_request_has_required_fields(self) -> None:
        """UnifiedModelRequest 必须包含 attempt_id/call_key/messages/max_output_tokens。"""
        req = _make_request()
        assert req["attempt_id"] == "attempt-1"
        assert req["call_key"] == "test-call-1"
        assert isinstance(req["messages"], list)
        assert len(req["messages"]) >= 1
        assert req["max_output_tokens"] == 100
        assert req["timeout_seconds"] == 30

    def test_unified_model_response_has_required_fields(self) -> None:
        """UnifiedModelResponse 必须包含 call_id/status/model_profile_id/usage/error。"""
        resp: UnifiedModelResponse = UnifiedModelResponse(
            call_id="c1",
            status="COMPLETED",
            model_profile_id="m1",
            provider_request_id=None,
            message=CanonicalMessage(role="assistant", content="ok"),
            tool_calls=[],
            usage=ModelUsage(
                input_tokens=1,
                output_tokens=2,
                cached_input_tokens=0,
                estimated_cost="0",
                currency="USD",
            ),
            latency_ms=10,
            finish_reason="stop",
            error=None,
        )
        assert resp["call_id"] == "c1"
        assert resp["status"] == "COMPLETED"
        assert resp["model_profile_id"] == "m1"
        assert resp["usage"]["input_tokens"] == 1
        assert resp["error"] is None

    def test_canonical_message_has_role_and_content(self) -> None:
        """CanonicalMessage 必须包含 role 与 content。"""
        msg = CanonicalMessage(role="user", content="hello")
        assert msg["role"] == "user"
        assert msg["content"] == "hello"

    def test_model_usage_has_token_fields(self) -> None:
        """ModelUsage 必须包含 input_tokens/output_tokens/cached_input_tokens。"""
        usage = ModelUsage(
            input_tokens=10,
            output_tokens=20,
            cached_input_tokens=5,
            estimated_cost="0.001",
            currency="USD",
        )
        assert usage["input_tokens"] == 10
        assert usage["output_tokens"] == 20
        assert usage["cached_input_tokens"] == 5


# --------------------------------------------------------------------------- #
# 验收 3 & 6：MockProviderAdapter 无网络调用
# --------------------------------------------------------------------------- #


class TestMockProviderAdapter:
    """``MockProviderAdapter`` 返回固定成功响应，不发起网络调用。"""

    def test_invoke_returns_completed(self) -> None:
        adapter = MockProviderAdapter()
        resp = asyncio.run(adapter.invoke(_make_connection(), "mock-model", _make_request()))
        assert resp["status"] == "COMPLETED"
        assert resp["model_profile_id"] == "mock-model"
        assert resp["message"]["content"] == "mock-response"
        assert resp["usage"]["input_tokens"] == 1
        assert resp["error"] is None

    def test_invoke_records_request_and_count(self) -> None:
        adapter = MockProviderAdapter()
        req = _make_request()
        asyncio.run(adapter.invoke(_make_connection(), "m", req))
        asyncio.run(adapter.invoke(_make_connection(), "m", req))
        assert adapter.invoke_count == 2
        assert adapter.last_request == req

    def test_custom_response_text(self) -> None:
        adapter = MockProviderAdapter(response_text="hello world")
        resp = asyncio.run(adapter.invoke(_make_connection(), "m", _make_request()))
        assert resp["message"]["content"] == "hello world"

    def test_probe_returns_ok_without_network(self) -> None:
        adapter = MockProviderAdapter()
        result = asyncio.run(adapter.probe(_make_connection()))
        assert result["ok"] is True
        assert result["provider"] == "mock"
        assert "latency_ms" in result

    def test_list_models_returns_list(self) -> None:
        adapter = MockProviderAdapter()
        models = asyncio.run(adapter.list_models(_make_connection()))
        assert isinstance(models, list)
        assert len(models) >= 1
        assert "name" in models[0]
        assert "context_window" in models[0]

    def test_stream_yields_chunks_without_network(self) -> None:
        adapter = MockProviderAdapter(response_text="a b c")

        async def _collect() -> list[dict[str, Any]]:
            return [chunk async for chunk in adapter.stream(_make_connection(), "m", _make_request())]

        chunks = asyncio.run(_collect())
        assert len(chunks) == 3
        assert all("delta" in c for c in chunks)

    def test_normalize_error_returns_stable_code(self) -> None:
        adapter = MockProviderAdapter()
        result = adapter.normalize_error(RuntimeError("boom"))
        assert result["code"] == "MOCK_ERROR"
        assert result["retryable"] is False
        assert "category" in result

    def test_no_httpx_client_attribute(self) -> None:
        """Mock Adapter 不持有 httpx.AsyncClient 等网络客户端属性。"""
        adapter = MockProviderAdapter()
        assert not hasattr(adapter, "_client")
        assert not hasattr(adapter, "_http_client")
        # 确保不导入 httpx 实例
        for attr in vars(adapter):
            assert not isinstance(getattr(adapter, attr, None), httpx.AsyncClient)

    def test_connection_credential_redacted_in_last_connection(self) -> None:
        """Mock Adapter 记录的 last_connection 中 api_key 被脱敏。"""
        adapter = MockProviderAdapter()
        conn = _make_connection(api_key=_FAKE_API_KEY)
        asyncio.run(adapter.probe(conn))
        assert adapter.last_connection is not None
        # api_key 必须被脱敏，不保留明文
        assert adapter.last_connection.get("api_key") == "***REDACTED***"
        assert _FAKE_API_KEY not in str(adapter.last_connection)


# --------------------------------------------------------------------------- #
# 验收 4：ProviderAdapterFactory 按 provider 名称创建 Adapter
# --------------------------------------------------------------------------- #


class TestProviderAdapterFactory:
    """``ProviderAdapterFactory`` 创建与注册。"""

    def test_create_openai_adapter(self) -> None:
        factory = ProviderAdapterFactory()
        adapter = factory.create_adapter("openai", _make_connection())
        assert isinstance(adapter, OpenAICompatibleAdapter)
        assert adapter.adapter_type == "openai_compatible"

    def test_create_anthropic_adapter(self) -> None:
        factory = ProviderAdapterFactory()
        adapter = factory.create_adapter("anthropic", _make_connection())
        assert isinstance(adapter, AnthropicProviderAdapter)
        assert adapter.adapter_type == "anthropic"

    def test_create_mock_adapter(self) -> None:
        factory = ProviderAdapterFactory()
        adapter = factory.create_adapter("mock", _make_connection())
        assert isinstance(adapter, MockProviderAdapter)
        assert adapter.adapter_type == "mock"

    @pytest.mark.parametrize(
        "provider",
        ["openai", "openai_compatible", "anthropic", "azure", "local", "mock"],
    )
    def test_all_default_providers_registered(self, provider: str) -> None:
        """默认注册的 provider 覆盖 OpenAI/Anthropic/Azure/local/mock。"""
        factory = ProviderAdapterFactory()
        assert factory.is_registered(provider)
        adapter = factory.create_adapter(provider, _make_connection())
        assert adapter is not None
        assert adapter.adapter_type

    def test_unknown_provider_raises_unsupported(self) -> None:
        factory = ProviderAdapterFactory()
        with pytest.raises(UnsupportedOperationError):
            factory.create_adapter("unknown_provider", _make_connection())

    def test_empty_provider_raises_unsupported(self) -> None:
        factory = ProviderAdapterFactory()
        with pytest.raises(UnsupportedOperationError):
            factory.create_adapter("", _make_connection())

    def test_register_custom_adapter(self) -> None:
        factory = ProviderAdapterFactory()

        class _CustomAdapter(MockProviderAdapter):
            adapter_type = "custom"

        factory.register_adapter("custom", lambda cfg: _CustomAdapter(connection_config=cfg))
        assert factory.is_registered("custom")
        adapter = factory.create_adapter("custom", _make_connection())
        assert isinstance(adapter, _CustomAdapter)
        assert adapter.adapter_type == "custom"

    def test_register_adapter_overrides_existing(self) -> None:
        factory = ProviderAdapterFactory()
        # 覆盖默认 mock 注册
        factory.register_adapter("mock", lambda cfg: MockProviderAdapter(response_text="overridden"))
        adapter = factory.create_adapter("mock", _make_connection())
        assert isinstance(adapter, MockProviderAdapter)

    def test_register_non_callable_raises(self) -> None:
        factory = ProviderAdapterFactory()
        with pytest.raises(UnsupportedOperationError):
            factory.register_adapter("bad", "not callable")  # type: ignore[arg-type]

    def test_list_registered_returns_sorted(self) -> None:
        factory = ProviderAdapterFactory()
        registered = factory.list_registered()
        assert registered == sorted(registered)
        assert "openai" in registered
        assert "anthropic" in registered
        assert "mock" in registered

    def test_get_default_factory_returns_singleton(self) -> None:
        f1 = get_default_factory()
        f2 = get_default_factory()
        assert f1 is f2

    def test_factory_does_not_access_secret_service(self) -> None:
        """工厂创建 Adapter 时不访问 SecretService（凭据从 connection_config 注入）。"""
        factory = ProviderAdapterFactory()
        # connection_config 已包含已解析的 api_key；工厂不调用任何 secret 解析
        conn = _make_connection(api_key="already-resolved-key")
        adapter = factory.create_adapter("openai", conn)
        # Adapter 应能从 connection_config 获取 api_key，不需要 SecretService
        assert adapter is not None


# --------------------------------------------------------------------------- #
# 验收 5：凭据从 connection_config 注入，不在 Adapter 内访问 SecretService
# --------------------------------------------------------------------------- #


class TestCredentialInjection:
    """凭据经 ``connection_config["api_key"]`` 注入。"""

    def test_openai_adapter_receives_api_key_in_config(self) -> None:
        adapter = OpenAICompatibleAdapter(
            connection_config=_make_connection(api_key=_FAKE_API_KEY)
        )
        # 内部默认 connection 应包含 api_key（但不暴露）
        assert adapter._default_connection.get("api_key") == _FAKE_API_KEY

    def test_anthropic_adapter_receives_api_key_in_config(self) -> None:
        adapter = AnthropicProviderAdapter(
            connection_config=_make_connection(api_key=_FAKE_API_KEY)
        )
        assert adapter._default_connection.get("api_key") == _FAKE_API_KEY

    def test_per_call_connection_overrides_default(self) -> None:
        """调用时传入的 connection 覆盖构造时的默认。"""
        adapter = OpenAICompatibleAdapter(
            connection_config=_make_connection(api_key="old-key")
        )
        merged = adapter._merge_connection(_make_connection(api_key="new-key"))
        assert merged["api_key"] == "new-key"

    def test_factory_does_not_resolve_secrets(self) -> None:
        """工厂模块不导入 SecretService。"""
        import maf_server.gateway.model.adapters as adapters_mod

        # 检查模块的导入：不应从 gateway.secrets 导入
        source = inspect.getsource(adapters_mod)
        # 移除 docstring 后检查实际 import 语句
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("from ", "import "))
        ]
        secrets_imports = [
            line for line in import_lines if "gateway.secrets" in line or "SecretService" in line
        ]
        assert not secrets_imports, (
            f"工厂模块不应导入 SecretService，发现：{secrets_imports}"
        )

    def test_adapter_does_not_import_secret_service(self) -> None:
        """Adapter 模块不导入 SecretService。"""
        import maf_provider_adapters.anthropic_adapter as anthropic_mod
        import maf_provider_adapters.mock_adapter as mock_mod
        import maf_provider_adapters.openai_compatible_adapter as openai_mod

        for module in (mock_mod, openai_mod, anthropic_mod):
            source = inspect.getsource(module)
            import_lines = [
                line.strip()
                for line in source.splitlines()
                if line.strip().startswith(("from ", "import "))
            ]
            secrets_imports = [
                line
                for line in import_lines
                if "gateway.secrets" in line or "SecretService" in line
            ]
            assert not secrets_imports, (
                f"{module.__name__} 不应导入 SecretService，发现：{secrets_imports}"
            )


# --------------------------------------------------------------------------- #
# 验收 7：Provider 错误统一为 code/category/retryable
# --------------------------------------------------------------------------- #


class TestErrorNormalization:
    """``normalize_error`` 输出稳定 ``code``/``category``/``retryable``。"""

    def test_mock_normalize_error_has_three_fields(self) -> None:
        adapter = MockProviderAdapter()
        result = adapter.normalize_error(ValueError("err"))
        assert "code" in result
        assert "category" in result
        assert "retryable" in result

    def test_openai_timeout_is_retryable_server(self) -> None:
        adapter = OpenAICompatibleAdapter()
        result = adapter.normalize_error(httpx.TimeoutException("timed out"))
        assert result["code"] == "TIMEOUT"
        assert result["retryable"] is True
        assert result["category"] == "server"

    def test_openai_connect_error_is_retryable_server(self) -> None:
        adapter = OpenAICompatibleAdapter()
        result = adapter.normalize_error(httpx.ConnectError("conn refused"))
        assert result["code"] == "CONNECT_FAILED"
        assert result["retryable"] is True
        assert result["category"] == "server"

    def test_anthropic_timeout_is_retryable_server(self) -> None:
        adapter = AnthropicProviderAdapter()
        result = adapter.normalize_error(httpx.TimeoutException("timed out"))
        assert result["code"] == "TIMEOUT"
        assert result["retryable"] is True

    def test_unknown_error_falls_back_to_provider_error(self) -> None:
        adapter = OpenAICompatibleAdapter()
        result = adapter.normalize_error(RuntimeError("something broke"))
        assert result["code"] == "PROVIDER_ERROR"
        assert result["retryable"] is True
        assert result["category"] == "server"

    def test_error_message_strips_api_key(self) -> None:
        """异常 message 含 api_key 字样时被脱敏为 provider error。"""
        adapter = OpenAICompatibleAdapter()
        result = adapter.normalize_error(
            RuntimeError("api_key=sk-secret-value-here is invalid")
        )
        assert "sk-secret-value-here" not in result["message"]
        assert "api_key" not in result["message"].lower()

    def test_anthropic_error_message_strips_token(self) -> None:
        adapter = AnthropicProviderAdapter()
        result = adapter.normalize_error(
            RuntimeError("token=abc123 authorization failed")
        )
        assert "abc123" not in result["message"]
        assert "token" not in result["message"].lower()


# --------------------------------------------------------------------------- #
# 验收 7 补充：OpenAI/Anthropic Adapter 结构与归一化（不发起真实网络）
# --------------------------------------------------------------------------- #


class TestOpenAICompatibleAdapterStructure:
    """``OpenAICompatibleAdapter`` 结构与归一化逻辑（不调用真实网络）。"""

    def test_invoke_without_credential_returns_failed(self) -> None:
        """缺少 api_key 时返回 FAILED 响应而非抛异常。"""
        adapter = OpenAICompatibleAdapter()
        # connection 不含 api_key，且 api_base 指向不可达地址
        conn = {"api_base": "https://invalid.localhost.invalid/v1"}
        resp = asyncio.run(adapter.invoke(conn, "gpt-4o", _make_request()))
        assert resp["status"] == "FAILED"
        assert resp["error"] is not None
        assert "code" in resp["error"]
        assert "retryable" in resp["error"]
        assert "category" in resp["error"]

    def test_invoke_response_does_not_leak_api_key(self) -> None:
        """失败响应不得包含 api_key 明文。"""
        adapter = OpenAICompatibleAdapter()
        conn = _make_connection(
            api_key=_FAKE_API_KEY,
            api_base="https://invalid.localhost.invalid/v1",
        )
        resp = asyncio.run(adapter.invoke(conn, "gpt-4o", _make_request()))
        # api_key 不得出现在响应任何字段
        resp_str = json.dumps(resp, default=str)
        assert _FAKE_API_KEY not in resp_str

    def test_probe_without_credential_returns_not_ok(self) -> None:
        adapter = OpenAICompatibleAdapter()
        conn = {"api_base": "https://invalid.localhost.invalid/v1"}
        result = asyncio.run(adapter.probe(conn))
        assert result["ok"] is False
        assert "error" in result

    def test_list_models_failure_returns_empty(self) -> None:
        """list_models 失败时返回空列表而非抛异常。"""
        adapter = OpenAICompatibleAdapter()
        conn = {"api_base": "https://invalid.localhost.invalid/v1"}
        models = asyncio.run(adapter.list_models(conn))
        assert models == []

    def test_build_chat_payload_maps_unified_request(self) -> None:
        """UnifiedModelRequest 正确映射为 OpenAI payload。"""
        payload = OpenAICompatibleAdapter._build_chat_payload(
            "gpt-4o", _make_request(), stream=False
        )
        assert payload["model"] == "gpt-4o"
        assert payload["stream"] is False
        assert isinstance(payload["messages"], list)
        assert payload["max_tokens"] == 100
        assert payload["temperature"] == 0.7

    def test_parse_chat_response_extracts_content(self) -> None:
        """OpenAI 响应正确归一化为 UnifiedModelResponse。"""
        body = {
            "id": "chatcmpl-1",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "hello there"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }
        resp = OpenAICompatibleAdapter._parse_chat_response(body, "c1", "gpt-4o", 10)
        assert resp["status"] == "COMPLETED"
        assert resp["message"]["content"] == "hello there"
        assert resp["usage"]["input_tokens"] == 5
        assert resp["usage"]["output_tokens"] == 3
        assert resp["provider_request_id"] == "chatcmpl-1"
        assert resp["finish_reason"] == "stop"


class TestAnthropicProviderAdapterStructure:
    """``AnthropicProviderAdapter`` 结构与归一化逻辑（不调用真实网络）。"""

    def test_invoke_without_credential_returns_failed(self) -> None:
        adapter = AnthropicProviderAdapter()
        conn = {"api_base": "https://invalid.localhost.invalid"}
        resp = asyncio.run(adapter.invoke(conn, "claude-3", _make_request()))
        assert resp["status"] == "FAILED"
        assert resp["error"] is not None
        assert "code" in resp["error"]

    def test_invoke_response_does_not_leak_api_key(self) -> None:
        adapter = AnthropicProviderAdapter()
        conn = _make_connection(
            api_key=_FAKE_API_KEY,
            api_base="https://invalid.localhost.invalid",
        )
        resp = asyncio.run(adapter.invoke(conn, "claude-3", _make_request()))
        resp_str = json.dumps(resp, default=str)
        assert _FAKE_API_KEY not in resp_str

    def test_list_models_returns_known_models(self) -> None:
        """Anthropic 未提供 list 端点，返回固定已知模型列表。"""
        adapter = AnthropicProviderAdapter()
        models = asyncio.run(adapter.list_models(_make_connection()))
        assert isinstance(models, list)
        assert len(models) >= 1
        assert all("name" in m for m in models)
        # 至少包含一个 claude 模型
        assert any("claude" in m["name"] for m in models)

    def test_build_messages_payload_separates_system(self) -> None:
        """Anthropic payload 把 system 消息单独提取到 system 字段。"""
        request = UnifiedModelRequest(
            attempt_id="a1",
            call_key="c1",
            model_policy_id="p1",
            messages=[
                CanonicalMessage(role="system", content="you are helpful"),
                CanonicalMessage(role="user", content="hi"),
            ],
            tools=[],
            response_schema=None,
            temperature=None,
            max_output_tokens=50,
            timeout_seconds=30,
            metadata={},
        )
        payload = AnthropicProviderAdapter._build_messages_payload(
            "claude-3", request, stream=False
        )
        assert payload["system"] == "you are helpful"
        assert all(m["role"] != "system" for m in payload["messages"])
        assert payload["max_tokens"] == 50

    def test_parse_messages_response_extracts_text(self) -> None:
        body = {
            "id": "msg_1",
            "content": [{"type": "text", "text": "hello from claude"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        resp = AnthropicProviderAdapter._parse_messages_response(
            body, "c1", "claude-3", 10
        )
        assert resp["status"] == "COMPLETED"
        assert resp["message"]["content"] == "hello from claude"
        assert resp["usage"]["input_tokens"] == 10
        assert resp["usage"]["output_tokens"] == 5
        assert resp["finish_reason"] == "end_turn"

    def test_auth_headers_use_x_api_key(self) -> None:
        """Anthropic 使用 x-api-key 头，不使用 Authorization Bearer。"""
        headers = AnthropicProviderAdapter._auth_headers(
            _FAKE_API_KEY, _make_connection()
        )
        assert headers["x-api-key"] == _FAKE_API_KEY
        assert "anthropic-version" in headers
        assert "Authorization" not in headers

    def test_openai_auth_headers_use_bearer(self) -> None:
        """OpenAI 兼容 Adapter 使用 Authorization Bearer 头。"""
        headers = OpenAICompatibleAdapter._auth_headers(
            _FAKE_API_KEY, _make_connection()
        )
        assert headers["Authorization"] == f"Bearer {_FAKE_API_KEY}"
        assert "x-api-key" not in headers


# --------------------------------------------------------------------------- #
# 验收 7 补充：错误码映射覆盖主要 HTTP 状态
# --------------------------------------------------------------------------- #


class TestHttpErrorMapping:
    """HTTP 状态码 → 稳定 code/category/retryable 映射。"""

    def test_openai_401_is_not_retryable_client(self) -> None:
        err = OpenAICompatibleAdapter._http_error(401, "", "https://api.example.com")
        assert err["code"] == "AUTHENTICATION_FAILED"
        assert err["retryable"] is False
        assert err["category"] == "client"

    def test_openai_429_is_retryable_server(self) -> None:
        err = OpenAICompatibleAdapter._http_error(429, "", "https://api.example.com")
        assert err["code"] == "RATE_LIMITED"
        assert err["retryable"] is True
        assert err["category"] == "server"

    def test_openai_500_is_retryable_server(self) -> None:
        err = OpenAICompatibleAdapter._http_error(500, "", "https://api.example.com")
        assert err["retryable"] is True
        assert err["category"] == "server"

    def test_anthropic_429_parses_error_type(self) -> None:
        """Anthropic 429 响应含 error.type 时优先解析。"""
        body = json.dumps({"error": {"type": "rate_limit_error", "message": "slow down"}})
        err = AnthropicProviderAdapter._http_error(429, body)
        assert err["code"] == "RATE_LIMITED"
        assert err["retryable"] is True

    def test_anthropic_overloaded_error_is_retryable(self) -> None:
        body = json.dumps({"error": {"type": "overloaded_error"}})
        err = AnthropicProviderAdapter._http_error(529, body)
        assert err["code"] == "PROVIDER_OVERLOADED"
        assert err["retryable"] is True
        assert err["category"] == "server"

    def test_http_error_message_has_no_api_key(self) -> None:
        err = OpenAICompatibleAdapter._http_error(401, "body with api_key=secret", "https://api.example.com")
        assert "api_key" not in err["message"].lower()
        assert "secret" not in err["message"].lower()


# --------------------------------------------------------------------------- #
# 验收：OpenAICompatibleAdapter 支持 OpenAI 兼容供应商路由
# --------------------------------------------------------------------------- #


class TestProviderRouting:
    """OpenAI 兼容供应商（GLM/DeepSeek/MiniMax/Kimi）可按配置路由。"""

    @pytest.mark.parametrize(
        "provider,api_base,adapter_type",
        [
            ("openai", "https://api.openai.com/v1", "openai_compatible"),
            ("openai_compatible", "https://api.deepseek.com/v1", "openai_compatible"),
            ("anthropic", "https://api.anthropic.com", "anthropic"),
            ("azure", "https://my-resource.openai.azure.com", "openai_compatible"),
            ("local", "http://localhost:11434/v1", "openai_compatible"),
            ("mock", "https://mock.local", "mock"),
        ],
    )
    def test_factory_routes_provider_to_correct_adapter(
        self, provider: str, api_base: str, adapter_type: str
    ) -> None:
        """Factory 按 provider 名称路由到正确 Adapter 类型。"""
        factory = ProviderAdapterFactory()
        adapter = factory.create_adapter(
            provider, _make_connection(api_base=api_base)
        )
        assert adapter.adapter_type == adapter_type

    def test_openai_compatible_adapter_accepts_custom_api_base(self) -> None:
        """OpenAICompatibleAdapter 接受任意 api_base（GLM/DeepSeek/MiniMax 端点）。"""
        for base in (
            "https://api.openai.com/v1",
            "https://api.deepseek.com/v1",
            "https://open.bigmodel.cn/api/paas/v4",
            "https://api.minimax.chat/v1",
            "https://api.moonshot.cn/v1",
        ):
            adapter = OpenAICompatibleAdapter(
                connection_config=_make_connection(api_base=base)
            )
            merged = adapter._merge_connection(_make_connection(api_base=base))
            assert base in merged["api_base"]
