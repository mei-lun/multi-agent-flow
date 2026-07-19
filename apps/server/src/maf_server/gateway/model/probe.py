"""模型连接分层验证编排服务（TASK-039）。

TASK-039 范围：
- 在 ``gateway/model/`` 增量添加 ``ModelProbeService``，编排模型连接的分层验证
  （config → credential → network → model）；
- 定义 ``ProbeResult``/``LayerResult``/``VerificationResult`` 三个 TypedDict；
- ``ModelProbeService`` 注入 ``ProviderAdapterFactory``/``SecretService``/
  ``SqliteModelConnectionRepository``，调用 Adapter 的 ``probe`` 与 ``list_models``；
- 凭据明文经 ``SecretService.resolve`` 短暂存在于 ``verify`` 调用栈内，绝不进入
  返回值、日志或异常。

设计依据：
- 《多 Agent 协同工具系统设计文档》§11.3 ProviderAdapter、§25.1 凭据安全。
- TASK-039 验收：前级失败后依赖检查标 SKIP；结果包含耗时和脱敏错误；
  重复调用不重复产生推理费用（probe 使用 ``list_models`` 等轻量端点，不调用
  ``invoke``）。

分层顺序（任一层失败则后继层标 SKIP，不执行实际探测）：
    1. config：provider/model_id/api_base 非空，api_base 为合法 http/https URL；
    2. credential：SecretService.resolve 成功，明文长度 >= 8 且符合前缀约定；
    3. network：Adapter.probe 返回 ``ok=True``（或 ``reachable=True``）；
    4. model：``model_id`` 在 ``Adapter.list_models`` 返回的模型名列表中；
       provider 不支持 list（返回空列表）时宽松通过。

安全约束：
- ``VerificationResult``/``LayerResult``/``ProbeResult`` 不含凭据明文；
- ``LayerResult.details`` 只包含脱敏字段（model_id、api_base host、latency_ms、
  available_models 列表）；
- 失败时 ``error`` 经 ``_redact_error`` 脱敏，不含 ``api_key``/``token``/``bearer``
  字样及其后的值；
- 凭据明文 ``plaintext`` 只在 ``verify`` 调用栈的局部变量中短暂存在，方法返回前
  被覆盖为空串。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, TypedDict
from urllib.parse import urlparse

from maf_server.core.clock import Clock
from maf_server.gateway.model.adapters import ProviderAdapterFactory
from maf_server.gateway.secrets.service import SecretService
from maf_server.modules.model_connections.repository import (
    ModelConnectionRecord,
    SqliteModelConnectionRepository,
)
from maf_server.modules.model_connections.schemas import ALLOWED_PROVIDERS


# --------------------------------------------------------------------------- #
# TypedDicts
# --------------------------------------------------------------------------- #


class LayerResult(TypedDict):
    """单层验证结果。

    - ``layer``：``"config"``/``"credential"``/``"network"``/``"model"``；
    - ``passed``：本层是否通过；SKIP 层为 ``False``；
    - ``details``：脱敏详情（如 ``{"model_id": "gpt-4", "latency_ms": 123}``）；
    - ``error``：失败或 SKIP 原因；成功时为 ``None``。
    """

    layer: str
    passed: bool
    details: dict[str, Any]
    error: str | None


class VerificationResult(TypedDict):
    """分层验证总结果。

    - ``connection_id``：被验证连接的 ID；
    - ``verified_at``：验证时间（带时区 ISO 8601）；
    - ``overall_passed``：所有层均通过（含非 SKIP 的执行层）时为 ``True``；
    - ``layers``：按执行顺序的 4 层结果；前级失败后继层标 SKIP。
    """

    connection_id: str
    verified_at: str
    overall_passed: bool
    layers: list[LayerResult]


class ProbeResult(TypedDict):
    """Adapter probe 归一化结果（网络层使用）。

    本 TypedDict 描述 ``ModelProbeService`` 从 Adapter ``probe`` 原始 dict
    归一化得到的视图；Adapter 自身仍返回 ``dict[str, Any]``（与 TASK-038 兼容），
    其中 ``ok`` 与 ``reachable`` 均被接受为可达性标志。

    - ``reachable``：远端是否可达（HTTP 200 或 list 端点可用）；
    - ``latency_ms``：probe 耗时（毫秒）；
    - ``available_models``：远端声明模型名列表（供模型层校验）；
    - ``error``：失败时的脱敏错误码/消息；成功时为 ``None``。
    """

    reachable: bool
    latency_ms: int
    available_models: list[str]
    error: str | None


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #


_LAYER_CONFIG: str = "config"
_LAYER_CREDENTIAL: str = "credential"
_LAYER_NETWORK: str = "network"
_LAYER_MODEL: str = "model"
_ALL_LAYERS: tuple[str, ...] = (
    _LAYER_CONFIG,
    _LAYER_CREDENTIAL,
    _LAYER_NETWORK,
    _LAYER_MODEL,
)

#: 凭据 purpose：与 ``LocalSecretService`` 默认白名单中的 ``probe`` 对齐。
_PROBE_PURPOSE: str = "probe"

#: 凭据明文最小长度（避免占位值通过校验）。
_MIN_CREDENTIAL_LENGTH: int = 8

#: ``api_key`` 类型凭据的前缀约定（OpenAI/Anthropic 等均以 ``sk-`` 开头）。
_API_KEY_PREFIX: str = "sk-"

#: 错误消息脱敏关键字（出现任一即整段替换为通用提示）。
_REDACT_KEYWORDS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "bearer",
    "authorization",
    "password",
)

# Values following credential-bearing fields are never safe to expose.  Keep
# the surrounding context (for example ``secret resolve failed``) when no
# value is present so callers can still diagnose which layer failed.
_SENSITIVE_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:api[_-]?key|password)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"(?:access[_-]?token|refresh[_-]?token|secret|credential)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\bbearer\s+\S+", re.IGNORECASE),
    re.compile(r"\bauthorization\s*:\s*\S+(?:\s+\S+)?", re.IGNORECASE),
)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


class _SystemClock:
    """默认使用系统 UTC 时钟；测试可注入虚拟时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def wait_until(self, deadline: datetime) -> None:
        return None


def _ensure_iso(value: datetime) -> str:
    """把 datetime 序列化为带时区 ISO 8601 字符串；naive 视为 UTC。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _is_valid_url(url: str) -> bool:
    """校验 ``api_base`` 是否为合法的 http/https URL（含 host 段）。"""
    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _redact_api_base(url: str) -> str:
    """脱敏 ``api_base``：只保留 scheme 与 host，移除 path/query。

    例：``https://api.openai.com/v1`` → ``https://api.openai.com``。
    """
    if not isinstance(url, str) or not url:
        return ""
    try:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return url.split("?", 1)[0] if "?" in url else url


def _redact_error(message: str) -> str:
    """脱敏错误消息：含敏感关键字时整段替换为通用提示。

    错误消息可能来自 Adapter 异常 ``str(exc)``，其中可能含 ``api_key=sk-xxx``
    等字样；统一替换避免明文泄漏。
    """
    if not isinstance(message, str) or not message:
        return ""
    lowered = message.lower()
    if any(pattern.search(message) for pattern in _SENSITIVE_VALUE_PATTERNS):
        return "provider error (redacted)"
    # A bare authentication keyword is still too revealing for transport
    # errors, while ordinary context such as ``secret resolve failed`` is
    # useful and contains no secret material.
    if any(kw in lowered for kw in _REDACT_KEYWORDS):
        return "provider error (redacted)"
    return message


def _extract_probe_reachable(probe_raw: Any) -> bool:
    """从 Adapter.probe 原始返回中提取可达性。

    兼容 TASK-038 的 ``ok`` 键与 TASK-039 文档建议的 ``reachable`` 键。
    """
    if not isinstance(probe_raw, dict):
        return False
    if "reachable" in probe_raw:
        return bool(probe_raw.get("reachable"))
    return bool(probe_raw.get("ok"))


def _extract_probe_latency(probe_raw: Any, fallback: int) -> int:
    """从 probe 原始返回中提取 latency_ms；缺失用 fallback。"""
    if isinstance(probe_raw, dict):
        val = probe_raw.get("latency_ms")
        if isinstance(val, (int, float)) and val >= 0:
            return int(val)
    return fallback


def _extract_probe_error(probe_raw: Any) -> str | None:
    """从 probe 原始返回中提取脱敏错误。"""
    if not isinstance(probe_raw, dict):
        return None
    err = probe_raw.get("error")
    if err is None:
        return None
    if isinstance(err, dict):
        # Adapter normalize_error 返回 {code, category, retryable, message}
        code = err.get("code")
        message = err.get("message")
        if code and message:
            return _redact_error(f"{code}: {message}")
        if code:
            return str(code)
        if message:
            return _redact_error(str(message))
        return "probe failed"
    if isinstance(err, str):
        return _redact_error(err)
    return None


# --------------------------------------------------------------------------- #
# ModelProbeService
# --------------------------------------------------------------------------- #


class ModelProbeService:
    """模型连接分层验证编排服务。

    依赖注入：
        - ``factory``：``ProviderAdapterFactory``，按 provider 创建 Adapter；
        - ``secret_service``：``SecretService``，解析凭据明文（短事务）；
        - ``repository``：``SqliteModelConnectionRepository``，读取连接记录
          （本服务不直接访问 DB，由调用方传入 ``ModelConnectionRecord``）；
        - ``clock``：``Clock``，可注入虚拟时钟用于测试。

    安全约束：
        - 凭据明文经 ``SecretService.resolve`` 在 ``verify`` 调用栈内短暂存在，
          方法返回前被覆盖为空串；绝不进入 ``VerificationResult``/``LayerResult``；
        - ``LayerResult.details`` 只包含脱敏字段（model_id、api_base host、
          latency_ms、available_models 列表）；
        - ``error`` 经 ``_redact_error`` 脱敏，不含 ``api_key`` 字样。

    谁调用它：
        ``ModelConnectionServiceImpl.verify_connection`` 在权限校验与记录加载之后
        调用本服务，传入 ``ModelConnectionRecord``；本服务完成 4 层验证后返回
        ``VerificationResult``，由 service 层更新连接状态。
    """

    def __init__(
        self,
        *,
        factory: ProviderAdapterFactory,
        secret_service: SecretService | None,
        repository: SqliteModelConnectionRepository | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._factory: ProviderAdapterFactory = factory
        self._secret_service: SecretService | None = secret_service
        self._repository: SqliteModelConnectionRepository = (
            repository or SqliteModelConnectionRepository()
        )
        self._clock: Clock = clock or _SystemClock()

    # ------------------------------------------------------------------ #
    # 公开方法
    # ------------------------------------------------------------------ #

    async def verify(self, record: ModelConnectionRecord) -> VerificationResult:
        """对单条连接记录执行 4 层验证；任一层失败后继层标 SKIP。

        凭据明文在 ``verify`` 调用栈内短暂存在，方法返回前被覆盖。
        ``record`` 由调用方加载（已在权限校验之后），本方法不访问数据库。

        :param record: 已加载的连接记录（含 ``credential_secret_id`` 引用）。
        :returns: ``VerificationResult``，含 4 层结果（含 SKIP）。
        """
        verified_at = _ensure_iso(self._clock.now())
        layers: list[LayerResult] = []

        # Layer 1: config
        config_result = self._verify_config(record)
        layers.append(config_result)
        if not config_result["passed"]:
            layers.extend(self._skip_remaining(_LAYER_CONFIG))
            return self._build_result(record, layers, verified_at)

        # Layer 2: credential
        cred_result, plaintext = await self._verify_credential(record)
        layers.append(cred_result)
        if not cred_result["passed"] or plaintext is None:
            layers.extend(self._skip_remaining(_LAYER_CREDENTIAL))
            return self._build_result(record, layers, verified_at)

        # Layer 3 & 4: network & model（需凭据明文，在 finally 中覆盖）
        try:
            network_result = await self._verify_network(record, plaintext)
            layers.append(network_result)
            if not network_result["passed"]:
                layers.extend(self._skip_remaining(_LAYER_NETWORK))
                return self._build_result(record, layers, verified_at)

            model_result = await self._verify_model(record, plaintext)
            layers.append(model_result)
        finally:
            # 明文离开作用域前覆盖，避免被后续异常处理或 GC 延迟读取。
            plaintext = ""  # type: ignore[assignment]

        return self._build_result(record, layers, verified_at)

    # ------------------------------------------------------------------ #
    # 各层实现
    # ------------------------------------------------------------------ #

    def _verify_config(self, record: ModelConnectionRecord) -> LayerResult:
        """配置层：检查 provider/model_id/api_base 非空且 api_base 合法。"""
        details: dict[str, Any] = {
            "provider": record.provider,
            "model_id": record.model_id,
            "api_base": _redact_api_base(record.api_base),
        }
        errors: list[str] = []
        if not record.provider:
            errors.append("provider 为空")
        elif record.provider not in ALLOWED_PROVIDERS:
            errors.append(
                f"provider 不在允许列表 {list(ALLOWED_PROVIDERS)}"
            )
        if not record.model_id:
            errors.append("model_id 为空")
        if not record.api_base:
            errors.append("api_base 为空")
        elif not _is_valid_url(record.api_base):
            errors.append("api_base 不是合法的 http/https URL")

        if errors:
            return LayerResult(
                layer=_LAYER_CONFIG,
                passed=False,
                details=details,
                error="; ".join(errors),
            )
        return LayerResult(
            layer=_LAYER_CONFIG,
            passed=True,
            details=details,
            error=None,
        )

    async def _verify_credential(
        self, record: ModelConnectionRecord
    ) -> tuple[LayerResult, str | None]:
        """凭据层：解析 secret 并校验明文格式。

        返回 ``(LayerResult, plaintext)``；失败时 ``plaintext=None``。
        明文仅在本调用栈内使用，调用方负责在 finally 中覆盖。
        """
        details: dict[str, Any] = {
            "credential_type": record.credential_type,
            "fingerprint": record.credential_fingerprint,
        }
        if self._secret_service is None:
            return (
                LayerResult(
                    layer=_LAYER_CREDENTIAL,
                    passed=False,
                    details=details,
                    error="SecretService 未注入",
                ),
                None,
            )
        try:
            plaintext = await self._secret_service.resolve(
                record.credential_secret_id, _PROBE_PURPOSE, record.id
            )
        except Exception as exc:  # noqa: BLE001 —— 解析失败转为层结果
            return (
                LayerResult(
                    layer=_LAYER_CREDENTIAL,
                    passed=False,
                    details=details,
                    error=_redact_error(
                        f"secret resolve 失败：{type(exc).__name__}"
                    ),
                ),
                None,
            )
        # 格式校验：长度 >= 8
        if not isinstance(plaintext, str) or len(plaintext) < _MIN_CREDENTIAL_LENGTH:
            return (
                LayerResult(
                    layer=_LAYER_CREDENTIAL,
                    passed=False,
                    details=details,
                    error=f"凭据明文格式不合法（长度不足 {_MIN_CREDENTIAL_LENGTH}）",
                ),
                None,
            )
        # 前缀校验：api_key 类型期望 ``sk-`` 前缀（大小写不敏感）
        if record.credential_type == "api_key" and not plaintext.lower().startswith(
            _API_KEY_PREFIX
        ):
            return (
                LayerResult(
                    layer=_LAYER_CREDENTIAL,
                    passed=False,
                    details=details,
                    error="凭据明文格式不合法（api_key 应以 'sk-' 开头）",
                ),
                None,
            )
        return (
            LayerResult(
                layer=_LAYER_CREDENTIAL,
                passed=True,
                details=details,
                error=None,
            ),
            plaintext,
        )

    async def _verify_network(
        self, record: ModelConnectionRecord, plaintext: str
    ) -> LayerResult:
        """网络层：调用 Adapter.probe 验证可达性。

        使用 ``GET /models`` 等轻量端点（OpenAICompatibleAdapter）或最小
        messages 调用（AnthropicProviderAdapter），不产生实际推理费用。
        """
        connection = self._build_connection(record, plaintext)
        details: dict[str, Any] = {"provider": record.provider}
        try:
            adapter = self._factory.create_adapter(record.provider, connection)
        except Exception as exc:  # noqa: BLE001 —— Adapter 创建失败转为层结果
            return LayerResult(
                layer=_LAYER_NETWORK,
                passed=False,
                details=details,
                error=_redact_error(
                    f"adapter 创建失败：{type(exc).__name__}"
                ),
            )
        start = time.monotonic()
        try:
            probe_raw = await adapter.probe(connection)
        except Exception as exc:  # noqa: BLE001 —— probe 异常转为层结果
            latency_ms = int((time.monotonic() - start) * 1000)
            details["latency_ms"] = latency_ms
            return LayerResult(
                layer=_LAYER_NETWORK,
                passed=False,
                details=details,
                error=_redact_error(f"probe 异常：{type(exc).__name__}"),
            )
        latency_ms = _extract_probe_latency(
            probe_raw, int((time.monotonic() - start) * 1000)
        )
        details["latency_ms"] = latency_ms
        reachable = _extract_probe_reachable(probe_raw)
        if reachable:
            return LayerResult(
                layer=_LAYER_NETWORK,
                passed=True,
                details=details,
                error=None,
            )
        err_msg = _extract_probe_error(probe_raw) or "probe 返回不可达"
        return LayerResult(
            layer=_LAYER_NETWORK,
            passed=False,
            details=details,
            error=err_msg,
        )

    async def _verify_model(
        self, record: ModelConnectionRecord, plaintext: str
    ) -> LayerResult:
        """模型层：验证 model_id 在 Adapter.list_models 返回列表中。

        宽松策略：``list_models`` 异常或返回空列表时视为通过，因为部分 provider
        （如 Anthropic）不提供 list 端点。仅当返回非空列表且 model_id 不在其中
        时才失败。
        """
        connection = self._build_connection(record, plaintext)
        details: dict[str, Any] = {"model_id": record.model_id}
        try:
            adapter = self._factory.create_adapter(record.provider, connection)
        except Exception as exc:  # noqa: BLE001
            return LayerResult(
                layer=_LAYER_MODEL,
                passed=False,
                details=details,
                error=_redact_error(
                    f"adapter 创建失败：{type(exc).__name__}"
                ),
            )
        try:
            models = await adapter.list_models(connection)
        except Exception:  # noqa: BLE001 —— list 异常宽松通过
            details["available_models"] = []
            details["note"] = "list_models 异常，宽松通过"
            return LayerResult(
                layer=_LAYER_MODEL,
                passed=True,
                details=details,
                error=None,
            )
        if not isinstance(models, list) or not models:
            details["available_models"] = []
            details["note"] = "provider 未返回模型列表，宽松通过"
            return LayerResult(
                layer=_LAYER_MODEL,
                passed=True,
                details=details,
                error=None,
            )
        names: list[str] = []
        for m in models:
            if isinstance(m, dict) and m.get("name"):
                names.append(str(m["name"]))
        details["available_models"] = names
        if record.model_id in names:
            return LayerResult(
                layer=_LAYER_MODEL,
                passed=True,
                details=details,
                error=None,
            )
        return LayerResult(
            layer=_LAYER_MODEL,
            passed=False,
            details=details,
            error=(
                f"model_id {record.model_id!r} 不在 provider 返回的模型列表中"
            ),
        )

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_connection(
        record: ModelConnectionRecord, plaintext: str
    ) -> dict[str, Any]:
        """构造 Adapter 调用所需的 connection dict（含已解析凭据）。"""
        return {
            "api_key": plaintext,
            "api_base": record.api_base,
            "model_id": record.model_id,
        }

    @staticmethod
    def _skip_remaining(failed_layer: str) -> list[LayerResult]:
        """为未执行的后续层生成 SKIP 标记。"""
        skipped: list[LayerResult] = []
        # 找到失败层在 _ALL_LAYERS 中的位置，其后继层全部 SKIP
        failed_idx = -1
        for idx, name in enumerate(_ALL_LAYERS):
            if name == failed_layer:
                failed_idx = idx
                break
        # 后继层（不含失败层本身）
        for name in _ALL_LAYERS[failed_idx + 1 :]:
            skipped.append(
                LayerResult(
                    layer=name,
                    passed=False,
                    details={},
                    error=f"SKIP: 前置层 {failed_layer!r} 失败，未执行",
                )
            )
        return skipped

    @staticmethod
    def _build_result(
        record: ModelConnectionRecord,
        layers: list[LayerResult],
        verified_at: str,
    ) -> VerificationResult:
        """构造 ``VerificationResult``。

        ``overall_passed`` 为 ``True`` 当且仅当所有层均通过（SKIP 层 passed=False
        会使 overall 为 False，与"前级失败后继 SKIP"语义一致）。
        """
        overall = all(layer["passed"] for layer in layers)
        return VerificationResult(
            connection_id=record.id,
            verified_at=verified_at,
            overall_passed=overall,
            layers=layers,
        )


__all__ = [
    "LayerResult",
    "ModelProbeService",
    "ProbeResult",
    "VerificationResult",
]

# TASK-040 measured profile API lives beside connection configuration but is
# re-exported here as the model gateway's public discovery surface.
from maf_server.modules.model_connections.capabilities import (  # noqa: E402
    CapabilityEvidence,
    MODEL_CAPABILITIES,
    ModelPolicyService,
    ModelPolicyVersion,
    ModelProfile,
    ModelProfileService,
)

__all__ += [
    "CapabilityEvidence",
    "MODEL_CAPABILITIES",
    "ModelPolicyService",
    "ModelPolicyVersion",
    "ModelProfile",
    "ModelProfileService",
]
