"""Sensitive field and host-path redaction.

脱敏处理器，覆盖以下敏感数据（见《接口设计与实现规范》第 4 节
"API 响应不得包含 API Key、Token、Secret 密文、宿主机绝对路径"）：

1. **敏感键名**：键名包含 ``api_key``、``token``、``password``、``secret``、
   ``credentials``、``authorization``、``cookie``、``passwd``、``pwd``、
   ``private_key``、``access_key``、``signing_key``、``master_key`` 等
   子串（大小写不敏感）的值会被替换为占位符；
2. **宿主机敏感路径**：字符串值中出现 Windows 用户目录
   （``C:\\Users\\<name>``）、Unix home 目录（``/home/<name>``、
   ``/Users/<name>``）、``~`` 展开、SSH/AWS/Kube 凭据目录
   （``.ssh``、``.aws``、``.kube``、``.gnupg``）、密钥文件名
   （``id_rsa``、``id_ed25519``、``*.pem``、``*.key``、``*.pfx``、
   ``*.p12``）会被替换为占位符。

``redact_sensitive`` 递归遍历 dict、list、tuple，对其他类型原样返回。
脱敏是不可逆的：返回的容器是新建的拷贝，不修改输入。

本模块不依赖 ``structlog``；``logger.py`` 把 ``redact_sensitive`` 包装为
structlog processor。
"""

from __future__ import annotations

import re
from collections.abc import MutableMapping
from typing import Any, Final

REDACTED_PLACEHOLDER: Final[str] = "***REDACTED***"

# --------------------------------------------------------------------------- #
# 敏感键名匹配
# --------------------------------------------------------------------------- #

#: 键名包含以下任一子串（大小写不敏感）即视为敏感键。
_SENSITIVE_KEY_SUBSTRINGS: Final[tuple[str, ...]] = (
    "api_key",
    "apikey",
    "token",
    "password",
    "passwd",
    "pwd",
    "secret",
    "credentials",
    "credential",
    "authorization",
    "auth_token",
    "cookie",
    "private_key",
    "access_key",
    "signing_key",
    "master_key",
    "session_secret",
    "api_secret",
    "client_secret",
    "refresh_token",
    "bearer",
)

#: 编译后的正则：键名（小写后）匹配任一子串。
_SENSITIVE_KEY_RE: Final[re.Pattern[str]] = re.compile(
    "|".join(re.escape(s) for s in _SENSITIVE_KEY_SUBSTRINGS),
    re.IGNORECASE,
)


def _is_sensitive_key(key: str) -> bool:
    """返回键名是否匹配敏感键规则。"""
    return bool(_SENSITIVE_KEY_RE.search(key))


# --------------------------------------------------------------------------- #
# 宿主机敏感路径匹配
# --------------------------------------------------------------------------- #

#: Windows 用户目录绝对路径，例如 ``C:\Users\alice\...``。
#: 同时匹配大小写盘符。``\`` 在字符串中需转义；正则中用 ``\\\\``。
_WIN_USER_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z]:\\Users\\",
)

#: Unix home 绝对路径，例如 ``/home/alice`` 或 ``/Users/alice``。
_UNIX_HOME_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[\s\"'`=,(])(/home/|/Users/)",
)

#: ``~`` 或 ``$HOME`` 展开形式，例如 ``~/.ssh/config`` 或 ``$HOME/.aws``。
_TILDE_HOME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[\s\"'`=,(])(~|\$HOME)(?=/|\\|$)",
)

#: 凭据目录名片段，匹配路径中出现的 ``.ssh``、``.aws``、``.kube``、``.gnupg``。
_CREDENTIAL_DIR_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[/\\])(\.ssh|\.aws|\.kube|\.gnupg|\.docker)(?:[/\\]|$)",
)

#: 密钥/证书文件名，匹配 ``id_rsa``、``id_ed25519``、``*.pem``、``*.key``、
#: ``*.pfx``、``*.p12``。
_KEY_FILE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[/\\])("
    r"id_rsa|id_ed25519|id_ecdsa|id_dsa"
    r"|[^/\\]+\.(?:pem|key|pfx|p12|keystore|jks)"
    r")(?:$|[\s\"'`,)])",
)

#: 全部敏感路径正则集合。任一命中即视为敏感路径。
_SENSITIVE_PATH_RES: Final[tuple[re.Pattern[str], ...]] = (
    _WIN_USER_PATH_RE,
    _UNIX_HOME_PATH_RE,
    _TILDE_HOME_RE,
    _CREDENTIAL_DIR_RE,
    _KEY_FILE_RE,
)


def _looks_like_sensitive_path(value: str) -> bool:
    """返回字符串值是否包含宿主机敏感路径片段。"""
    for pattern in _SENSITIVE_PATH_RES:
        if pattern.search(value):
            return True
    return False


# --------------------------------------------------------------------------- #
# 递归脱敏
# --------------------------------------------------------------------------- #


def redact_sensitive(value: Any) -> Any:
    """递归脱敏容器与值。

    - ``dict``：键名敏感时值替换为占位符；否则递归处理值；
    - ``list``/``tuple``：递归处理每个元素（返回 ``list``）；
    - ``str``：值本身匹配敏感路径时替换为占位符；
    - 其他类型：原样返回。

    返回的容器是新建的拷贝，不修改输入。脱敏不可逆。
    """
    return _redact(value, in_sensitive=False)


def _redact(value: Any, *, in_sensitive: bool) -> Any:
    """递归实现。

    ``in_sensitive`` 表示当前值位于敏感键下，应整体替换为占位符
    （但仍需遍历以保持容器结构，便于下游序列化）。
    """
    if in_sensitive:
        return REDACTED_PLACEHOLDER
    if isinstance(value, dict):
        return {
            k: _redact(v, in_sensitive=_is_sensitive_key(str(k)))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, in_sensitive=False) for item in value]
    if isinstance(value, tuple):
        # tuple 通常不可变；脱敏后返回 list 以保持一致行为。
        return [_redact(item, in_sensitive=False) for item in value]
    if isinstance(value, str):
        if _looks_like_sensitive_path(value):
            return REDACTED_PLACEHOLDER
        return value
    return value


# --------------------------------------------------------------------------- #
# structlog processor 包装
# --------------------------------------------------------------------------- #


def redact_processor(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor：脱敏 ``event_dict`` 中的敏感字段。

    放在 processor 链的渲染（``JSONRenderer``）之前。``event_dict`` 中
    ``exc_info`` 等由 ``structlog.processors.format_exc_info`` 产出的字符串
    也会被扫描；异常堆栈中的敏感路径同样会被替换为占位符。
    """
    return _redact(event_dict, in_sensitive=False)  # type: ignore[no-any-return]


__all__ = [
    "REDACTED_PLACEHOLDER",
    "redact_processor",
    "redact_sensitive",
]
