"""敏感配置 fixture：测试用脱敏配置，不含真实模型/GitHub Key。

所有 token/key 均为明显的 fake 值（带 ``FAKE``/``NOT_REAL`` 标记），
禁止提交真实凭据。配套 ``tests/fixtures/README.md`` 的脱敏约定。
"""

from __future__ import annotations

from typing import Any

# 明显的 fake 值，非真实凭据。测试断言这些值不包含真实 Key 特征。
FAKE_GITHUB_TOKEN = "ghp_FAKE_TOKEN_NOT_REAL_000000000000"
FAKE_MODEL_API_KEY = "sk-fake-key-not-real-0000000000000000"
FAKE_SSH_KEY_MATERIAL = "FAKE-SSH-PRIVATE-KEY-MATERIAL"

# 标记 fake 值的子串，便于断言「未使用真实凭据」。
_FAKE_MARKERS = ("FAKE", "NOT_REAL", "not-real", "not-for-production")


def is_fake_credential(value: str) -> bool:
    """判断一个字符串是否为明显的 fake 凭据（含 fake 标记）。"""
    upper = value.upper()
    return any(marker.upper() in upper for marker in _FAKE_MARKERS)


def safe_model_connection() -> dict[str, Any]:
    """不含真实 Key 的模型连接配置。

    仅存环境变量名（``api_key_env``），不存值；真实测试通过 mock adapter
    或显式注入 fake key 使用。
    """
    return {
        "provider": "mock",
        "base_url": "http://localhost:0",
        "api_key_env": "MAF_FAKE_MODEL_KEY",
        "model_name": "mock-model",
    }


def safe_git_binding(
    remote_url: str = "https://github.com/org/repo.git",
) -> dict[str, Any]:
    """不含真实凭据的 Git binding 配置。

    ``remote_url`` 指向公开示例仓库地址，``secret_id`` 为 fake 占位。
    """
    return {
        "remote_url": remote_url,
        "credential_type": "HTTPS_TOKEN",
        "secret_id": "fake-secret-id-not-real",
    }


__all__ = [
    "FAKE_GITHUB_TOKEN",
    "FAKE_MODEL_API_KEY",
    "FAKE_SSH_KEY_MATERIAL",
    "is_fake_credential",
    "safe_git_binding",
    "safe_model_connection",
]
