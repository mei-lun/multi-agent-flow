"""Secret 后端存储端口与核心抽象；业务层应优先调用 gateway.secrets.SecretService。

本模块只定义 ``SecretStore`` Protocol、主密钥加载/生成工具和轮换协调助手。
不导入 ``keyring``/``cryptography``（具体加密在后端实现中），不连接数据库，
不接触明文。明文只允许在 SecretService 边界短暂存在，并由具体 SecretStore
写入 OS Keyring 或 AES-GCM 加密文件；SQLite/Git 永不保存明文。

设计参考：《多 Agent 协同工具系统设计文档》23.2 Secret 存储——MVP 优先复用
操作系统凭据库，SQLite 只保存随机 account 引用；无可用 keyring backend 时
启用 AES-GCM fallback，Associated Data 绑定 organization_id + secret_id +
secret_type，防止密文跨记录替换。
"""

from __future__ import annotations

import os
import secrets as _token
from pathlib import Path
from typing import Protocol, runtime_checkable

from maf_domain.errors import ExternalDependencyError

#: AES-256 主密钥字节数。
MASTER_KEY_SIZE_BYTES: int = 32

#: opaque backend key 的随机 token 字节数（token_urlsafe 编码后约 22 字符）。
_BACKEND_TOKEN_BYTES: int = 16


@runtime_checkable
class SecretStore(Protocol):
    """Secret 后端存储端口。

    具体实现见 ``gateway.secrets.keyring_store.KeyringStore`` 与
    ``gateway.secrets.aes_gcm_store.AesGcmFileStore``。明文只经 ``create``/
    ``rotate`` 传入，密文存 Keyring 或 AES-GCM 加密文件；本 Protocol 不接触
    明文以外的持久化形式。
    """

    async def create(self, name: str, plaintext: str) -> str:
        """存储新值并返回 opaque backend key；不记录 plaintext。"""
        ...

    async def resolve(self, backend_key: str) -> str:
        """从 Keyring/AES-GCM 后端读取；调用权限在上层 SecretService 检查。"""
        ...

    async def rotate(self, backend_key: str, plaintext: str) -> None:
        """原子替换后端值；失败时旧值仍可用。"""
        ...

    async def revoke(self, backend_key: str) -> None:
        """幂等删除或吊销后端值。"""
        ...


# --------------------------------------------------------------------------- #
# 主密钥管理
# --------------------------------------------------------------------------- #


def generate_master_key(path: Path) -> None:
    """生成 32 字节随机主密钥并以受限权限写入 ``path``。

    若文件已存在则原样保留，避免破坏已加密数据。父目录由调用方创建。
    使用 ``O_CREAT | O_EXCL`` 防止符号链接竞争；POSIX 上权限收紧为 0600，
    Windows 上权限由部署方通过文件 ACL 控制（``chmod`` 为 no-op）。

    Windows 上 ``os.open`` 默认以文本模式打开文件，会把密钥中的 ``0A`` 字节
    翻译为 ``0D 0A``，破坏密钥长度。必须显式传入 ``O_BINARY`` 以二进制写入。
    """
    if path.exists():
        return
    key = _token.token_bytes(MASTER_KEY_SIZE_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    # Windows 默认文本模式会把 0A 翻译为 0D 0A，破坏随机密钥字节序列。
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        # 非 POSIX 平台或权限不足：忽略，部署方负责文件 ACL。
        pass


def load_master_key(path: Path) -> bytes:
    """从 ``path`` 读取主密钥并校验长度。

    文件不存在或长度不等于 ``MASTER_KEY_SIZE_BYTES`` 时抛
    ``ExternalDependencyError``（``retryable=True``，调用方应先
    ``generate_master_key``）。返回的 ``bytes`` 调用方应尽快从内存清除。
    """
    if not path.exists():
        raise ExternalDependencyError(
            f"master key file not found: {path}",
            context={"master_key_file": str(path)},
            retryable=True,
        )
    data = path.read_bytes()
    if len(data) != MASTER_KEY_SIZE_BYTES:
        raise ExternalDependencyError(
            f"master key must be {MASTER_KEY_SIZE_BYTES} bytes, got {len(data)}",
            context={"master_key_file": str(path), "actual_size": len(data)},
        )
    return data


def new_backend_key() -> str:
    """生成 opaque backend key（与明文无关的随机 token）。

    作为 keyring account 或 AES-GCM 文件名 stem；不可逆，不含明文。
    """
    return _token.token_urlsafe(_BACKEND_TOKEN_BYTES)


# --------------------------------------------------------------------------- #
# 轮换协调
# --------------------------------------------------------------------------- #


async def rotate_with_retention(
    store: SecretStore,
    backend_key: str,
    new_plaintext: str,
    *,
    name: str,
) -> str:
    """轮换协调：失败时旧值保留。

    策略（"先写新值成功再删旧值"）：先 ``store.create(name, new_plaintext)``
    写入新 backend key；成功后才 ``store.revoke(backend_key)`` 删除旧 key。
    若 ``create`` 失败，旧 ``backend_key`` 与旧明文保持可用，调用方仍可
    ``resolve`` 旧值。返回新 backend_key（调用方应更新引用）。

    若 ``revoke`` 失败，旧 key 残留但新值已生效，调用方应记录清理任务；
    本函数不抛此异常以保证新值可用性。

    本助手用于不支持原子 in-place ``rotate`` 的后端；``KeyringStore`` /
    ``AesGcmFileStore`` 的 ``rotate`` 已是原子替换，可直接调用。
    """
    new_key = await store.create(name, new_plaintext)
    try:
        await store.revoke(backend_key)
    except Exception:
        # 旧 key 残留：不影响新值生效，调用方应记录清理任务。
        pass
    return new_key


__all__ = [
    "MASTER_KEY_SIZE_BYTES",
    "SecretStore",
    "generate_master_key",
    "load_master_key",
    "new_backend_key",
    "rotate_with_retention",
]
