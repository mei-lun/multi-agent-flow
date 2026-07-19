"""模型连接配置管理的请求/响应字段定义。

TASK-037 范围：
- 定义 ``ModelConnectionService`` 对外暴露的 DTO（``ModelConnectionView``、
  ``CreateModelConnectionRequest``、``UpdateModelConnectionRequest``、``TestResult``）；
- 定义 provider / credential_type / status 取值常量。

安全约束：
- ``ModelConnectionView`` 只返回 ``credential_type`` 与不可逆 ``credential_fingerprint``，
  绝不返回凭据明文或 ``secret_id``；
- 时间字段为带时区 ISO 8601 字符串。
"""

from __future__ import annotations

from typing import Literal, TypedDict

# --------------------------------------------------------------------------- #
# 取值常量
# --------------------------------------------------------------------------- #

#: 支持的模型供应商。``local`` 表示本地推理端点（如 Ollama）。
ALLOWED_PROVIDERS: tuple[str, ...] = (
    "openai",
    "codex",
    "openai_compatible",
    "glm",
    "deepseek",
    "minimax",
    "kimi_code",
    "anthropic",
    "azure",
    "local",
)

#: 支持的凭据类型。
ALLOWED_CREDENTIAL_TYPES: tuple[str, ...] = (
    "api_key",
    "oauth_token",
    "bearer_token",
)

#: 连接初始状态：创建后未经过 ``test_connection`` 验证。
STATUS_UNVERIFIED: str = "UNVERIFIED"
#: ``test_connection`` 验证通过。
STATUS_VERIFIED: str = "VERIFIED"
#: ``test_connection`` 验证失败（凭据不可解析或 URL 非法）。
STATUS_ERROR: str = "ERROR"


Provider = Literal[
    "openai",
    "codex",
    "openai_compatible",
    "glm",
    "deepseek",
    "minimax",
    "kimi_code",
    "anthropic",
    "azure",
    "local",
]
CredentialType = Literal["api_key", "oauth_token", "bearer_token"]
ConnectionStatus = Literal["UNVERIFIED", "VERIFIED", "ERROR"]


# --------------------------------------------------------------------------- #
# 视图与请求 DTO
# --------------------------------------------------------------------------- #


class ModelConnectionView(TypedDict):
    """模型连接对外视图，不含凭据明文与 ``secret_id``。

    - ``credential_fingerprint``：不可逆指纹（``sha256(plaintext)[:8] + ".." + plaintext[-4:]``），
      与 TASK-029/032 的指纹算法一致，用于运维识别与脱敏展示。
    - ``status``：``UNVERIFIED`` / ``VERIFIED`` / ``ERROR``。
    - ``version``：乐观锁版本号。
    """

    id: str
    name: str
    provider: str
    model_id: str
    api_base: str
    credential_type: str
    credential_fingerprint: str | None
    status: str
    created_by: str
    created_at: str
    updated_at: str
    version: int


class CreateModelConnectionRequest(TypedDict):
    """创建模型连接请求。

    ``credential_value`` 为明文凭据，仅在本次请求内存中处理，经 SecretService
    存储后绝不持久化明文。
    """

    name: str
    provider: str
    model_id: str
    api_base: str
    credential_type: str
    credential_value: str
    idempotency_key: str


class UpdateModelConnectionRequest(TypedDict, total=False):
    """更新模型连接请求（部分更新）。

    至少提供 ``name`` / ``api_base`` / ``credential_value`` 之一。``expected_version``
    为必填的乐观锁版本号。``credential_value`` 非空时经 SecretService 轮换凭据。
    """

    name: str
    api_base: str
    credential_value: str
    expected_version: int
    idempotency_key: str


class TestResult(TypedDict):
    """``test_connection`` 返回的验证结果，不含凭据明文。

    - ``ok``：凭据可解析且 URL 格式正确时为 ``True``。
    - ``status``：验证后连接的新状态（``VERIFIED`` / ``ERROR``）。
    - ``message``：人类可读说明，不含敏感信息。
    - ``checked_at``：检查时间（带时区 ISO 8601）。
    """

    connection_id: str
    ok: bool
    status: str
    message: str
    checked_at: str


__all__ = [
    "ALLOWED_CREDENTIAL_TYPES",
    "ALLOWED_PROVIDERS",
    "ConnectionStatus",
    "CreateModelConnectionRequest",
    "CredentialType",
    "ModelConnectionView",
    "Provider",
    "STATUS_ERROR",
    "STATUS_UNVERIFIED",
    "STATUS_VERIFIED",
    "TestResult",
    "UpdateModelConnectionRequest",
]
