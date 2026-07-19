"""AES-GCM 文件后端实现（keyring 不可用时的 fallback）。

使用 32 字节 master_key 经 AES-256-GCM 加密 Secret 明文，密文 + nonce
写入 JSON 文件。Associated Data 绑定 ``organization_id + backend_key +
secret_type``，防止密文跨记录替换（设计文档 23.2）。轮换通过临时文件 +
``os.replace`` 实现原子替换，失败时旧文件保留。

明文只存在于内存；磁盘上只有 ``ciphertext``、``nonce``、``key_version``
和 AAD 绑定字段，以及非敏感的 ``name``/``secret_type``。SQLite/Git 永不
保存明文或 master_key。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from maf_domain.errors import ExternalDependencyError, NotFoundError

from maf_server.core.secrets import (
    MASTER_KEY_SIZE_BYTES,
    SecretStore,
    new_backend_key,
)

#: GCM nonce 长度（96 位，NIST SP 800-38D 推荐）。
_NONCE_SIZE = 12

#: 默认 secret_type（具体类型由 SecretService 透传，未来可扩展）。
_DEFAULT_SECRET_TYPE = "SECRET"


class AesGcmFileStore:
    """master_key AES-GCM 加密的文件后端。

    backend_key 是随机 token，作为文件名 stem。明文只存在于内存；
    磁盘上只有 ciphertext + nonce + key_version + AAD 绑定字段。
    """

    def __init__(
        self,
        master_key: bytes,
        storage_dir: Path,
        *,
        organization_id: str,
        key_version: int = 1,
    ) -> None:
        if len(master_key) != MASTER_KEY_SIZE_BYTES:
            raise ExternalDependencyError(
                f"master key must be {MASTER_KEY_SIZE_BYTES} bytes, "
                f"got {len(master_key)}",
                context={"actual_size": len(master_key)},
            )
        self._aes = AESGCM(master_key)
        # Tests and container manifests commonly use POSIX ``/tmp`` paths.
        # On Windows ``Path('/tmp/foo')`` becomes ``\\tmp\\foo`` (a root
        # path that is usually not writable), so map that conventional path
        # to the platform temporary directory while preserving its suffix.
        if os.name == "nt" and storage_dir.drive == "" and storage_dir.root == "\\":
            posix_path = storage_dir.as_posix()
            if posix_path == "/tmp" or posix_path.startswith("/tmp/"):
                storage_dir = Path(tempfile.gettempdir()) / posix_path.removeprefix("/tmp").lstrip("/")
        self._dir = storage_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._org_id = organization_id
        self._key_version = key_version

    # ------------------------------------------------------------------ #
    # 路径与 AAD
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_safe_token(token: str) -> bool:
        """backend_key 必须是 URL-safe token，禁止路径分隔符与 ``..``。"""
        if not token or len(token) > 128:
            return False
        return all(c.isalnum() or c in "-_" for c in token)

    def _path_for(self, backend_key: str) -> Path:
        if not self._is_safe_token(backend_key):
            raise NotFoundError(
                "invalid backend key",
                context={"backend_key": backend_key},
            )
        return self._dir / f"{backend_key}.json"

    def _aad(self, backend_key: str, secret_type: str) -> bytes:
        """构造 Associated Data，绑定组织、记录与类型，防密文跨记录替换。"""
        return f"{self._org_id}|{backend_key}|{secret_type}".encode("utf-8")

    # ------------------------------------------------------------------ #
    # SecretStore 实现
    # ------------------------------------------------------------------ #

    async def create(self, name: str, plaintext: str) -> str:
        backend_key = new_backend_key()
        self._write_record(backend_key, name, plaintext, _DEFAULT_SECRET_TYPE)
        return backend_key

    async def resolve(self, backend_key: str) -> str:
        path = self._path_for(backend_key)
        if not path.exists():
            raise NotFoundError(
                "secret file not found",
                context={"backend_key": backend_key},
            )
        record = self._read_record(path)
        nonce = bytes.fromhex(record["nonce"])
        ciphertext = bytes.fromhex(record["ciphertext"])
        aad = self._aad(backend_key, record["secret_type"])
        try:
            plaintext = self._aes.decrypt(nonce, ciphertext, aad)
        except InvalidTag as exc:
            raise ExternalDependencyError(
                "AES-GCM decryption failed (tampered ciphertext or wrong master key)",
                context={"backend_key": backend_key},
                retryable=False,
            ) from exc
        return plaintext.decode("utf-8")

    async def rotate(self, backend_key: str, plaintext: str) -> None:
        """原子替换：先校验存在，再经临时文件 + ``os.replace`` 覆盖写。

        失败时旧文件保留，调用方仍可 ``resolve`` 旧值。
        """
        path = self._path_for(backend_key)
        if not path.exists():
            raise NotFoundError(
                "cannot rotate missing secret",
                context={"backend_key": backend_key},
            )
        record = self._read_record(path)
        self._write_record(
            backend_key,
            record["name"],
            plaintext,
            record["secret_type"],
        )

    async def revoke(self, backend_key: str) -> None:
        """幂等删除；文件不存在视为已吊销。"""
        path = self._path_for(backend_key)
        if path.exists():
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    # ------------------------------------------------------------------ #
    # 内部：原子文件读写
    # ------------------------------------------------------------------ #

    def _read_record(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_record(
        self,
        backend_key: str,
        name: str,
        plaintext: str,
        secret_type: str,
    ) -> None:
        nonce = os.urandom(_NONCE_SIZE)
        aad = self._aad(backend_key, secret_type)
        ciphertext = self._aes.encrypt(nonce, plaintext.encode("utf-8"), aad)
        record: dict[str, Any] = {
            "name": name,
            "secret_type": secret_type,
            "nonce": nonce.hex(),
            "ciphertext": ciphertext.hex(),
            "key_version": self._key_version,
        }
        payload = json.dumps(record, ensure_ascii=False).encode("utf-8")
        path = self._path_for(backend_key)
        # 原子写：同目录临时文件 + os.replace（POSIX 与 Windows 均原子）。
        fd, tmp_name = tempfile.mkstemp(
            prefix=".secret-", suffix=".tmp", dir=str(self._dir)
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


__all__ = ["AesGcmFileStore"]
