"""本地 SecretService 具体实现：权限检查 + 审计 + 轮换协调。

明文只经 ``SecretStore.create``/``rotate`` 传入后端；metadata（secret_id、
backend_key、version、owner、status 等）保存在内存中，生产环境由 ``secrets``
SQLite 表（仅引用，无明文）替换。每次 ``resolve`` 写审计事件，但不写明文值；
审计日志经 ``redact_processor`` 二次保护，确保明文不进日志。

后端选择：构造时传入 ``primary``（通常 ``KeyringStore``）与可选 ``fallback``
（``AesGcmFileStore``）。每次调用若 primary 自报 ``is_available()`` 为 ``False``
则改用 fallback；运行期 keyring 失效时自动回退到 AES-GCM。

权限模型：默认仅 owner 可 ``resolve``；``permission_policy`` 可注入自定义
检查器（如 RBAC/ABAC），抛 ``PermissionDeniedError`` 表示拒绝。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import structlog

from maf_domain.errors import (
    NotFoundError,
    PermissionDeniedError,
    VersionConflictError,
)

from maf_server.core.secrets import SecretStore, new_backend_key
from maf_server.gateway.secrets.service import SecretService as SecretServiceProtocol

#: 默认允许的 resolve purpose 白名单。具体 secret 可在 create 时覆盖。
_DEFAULT_ALLOWED_PURPOSES: frozenset[str] = frozenset(
    {"model.invoke", "tool.call", "git.fetch", "mcp.call", "probe", "verify"}
)

#: Secret 状态机取值。
_STATUS_ACTIVE = "ACTIVE"
_STATUS_ROTATING = "ROTATING"
_STATUS_REVOKED = "REVOKED"


@dataclass
class SecretMetadata:
    """Secret 引用元数据（无明文）。

    生产环境对应 ``secrets`` SQLite 表；此处内存持有，仅含引用与状态。
    ``fingerprint`` 是不可逆指纹（sha256 前 8 位 + 明文后 4 位），用于识别
    重复与运维展示（设计文档 25.1：响应只返回指纹后四位，不返回 Key）。
    """

    secret_id: str
    backend_key: str
    name: str
    owner_type: str
    owner_id: str
    secret_type: str
    version: int
    status: str
    allowed_purposes: frozenset[str]
    fingerprint: str
    created_at: datetime
    last_used_at: datetime | None = None
    last_rotated_at: datetime | None = None


def _fingerprint(plaintext: str) -> str:
    """不可逆指纹：``sha256(plaintext)[:8]`` + 明文后 4 位。

    仅用于去重识别和运维展示；不构成明文泄漏（设计文档允许指纹后四位）。
    """
    digest = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    tail = plaintext[-4:] if len(plaintext) >= 4 else plaintext
    return f"{digest[:8]}..{tail}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _default_permission_policy(
    secret_id: str,
    purpose: str,
    actor_id: str,
    meta: SecretMetadata,
) -> None:
    """默认权限：仅 owner 可 resolve。"""
    if actor_id != meta.owner_id:
        raise PermissionDeniedError(
            f"actor {actor_id!r} is not the owner of secret {secret_id}",
            context={
                "secret_id": secret_id,
                "actor_id": actor_id,
                "owner_id": meta.owner_id,
            },
        )


#: 权限检查器签名：(secret_id, purpose, actor_id, meta) -> None，拒绝时抛异常。
PermissionPolicy = Callable[[str, str, str, SecretMetadata], None]


class LocalSecretService:
    """本地 SecretService：Keyring 优先、AES-GCM 回退。

    满足 ``SecretService`` Protocol；``create`` 额外接受 ``name``、
    ``secret_type``、``allowed_purposes`` 可选参数，便于配置模块定制。
    """

    def __init__(
        self,
        primary: SecretStore,
        fallback: SecretStore | None = None,
        *,
        permission_policy: PermissionPolicy | None = None,
        audit_logger: Any = None,
        allowed_purposes: frozenset[str] | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._permission_policy = permission_policy or _default_permission_policy
        self._log = (
            audit_logger if audit_logger is not None else structlog.get_logger("maf.secrets")
        )
        self._allowed_purposes = allowed_purposes or _DEFAULT_ALLOWED_PURPOSES
        self._metadata: dict[str, SecretMetadata] = {}

    # ------------------------------------------------------------------ #
    # SecretService Protocol 实现
    # ------------------------------------------------------------------ #

    async def create(
        self,
        owner_type: str,
        owner_id: str,
        plaintext: str,
        *,
        secret_type: str = "SECRET",
        name: str | None = None,
        allowed_purposes: frozenset[str] | None = None,
    ) -> str:
        """把明文写入后端（Keyring 优先，失败回退 AES-GCM），返回 secret_id。

        plaintext 不得写数据库明文字段、日志、异常、事件或返回值。失败时
        不创建 metadata 记录（``store.create`` 抛异常前不会写入 metadata）。
        """
        if not plaintext:
            raise ValueError("plaintext must not be empty")
        if not owner_id:
            raise ValueError("owner_id must not be empty")

        secret_id = new_backend_key()
        display_name = name or f"{owner_type}/{owner_id}"
        store = self._select_store()
        # store.create 失败则抛异常，下面 metadata 不写入，旧状态不变。
        backend_key = await store.create(display_name, plaintext)
        meta = SecretMetadata(
            secret_id=secret_id,
            backend_key=backend_key,
            name=display_name,
            owner_type=owner_type,
            owner_id=owner_id,
            secret_type=secret_type,
            version=1,
            status=_STATUS_ACTIVE,
            allowed_purposes=allowed_purposes or self._allowed_purposes,
            fingerprint=_fingerprint(plaintext),
            created_at=_now(),
        )
        self._metadata[secret_id] = meta
        self._audit(
            "secret.created",
            secret_id=secret_id,
            owner_type=owner_type,
            owner_id=owner_id,
            secret_type=secret_type,
            backend=type(store).__name__,
        )
        return secret_id

    async def resolve(self, secret_id: str, purpose: str, actor_id: str) -> str:
        """检查 owner/purpose 权限后返回明文；每次解析写审计但不写值。"""
        meta = self._require_active(secret_id)
        if purpose not in meta.allowed_purposes:
            raise PermissionDeniedError(
                f"purpose {purpose!r} not allowed for secret {secret_id}",
                context={
                    "secret_id": secret_id,
                    "purpose": purpose,
                    "actor_id": actor_id,
                },
            )
        self._permission_policy(secret_id, purpose, actor_id, meta)
        store = self._select_store()
        plaintext = await store.resolve(meta.backend_key)
        meta.last_used_at = _now()
        self._audit(
            "secret.resolved",
            secret_id=secret_id,
            purpose=purpose,
            actor_id=actor_id,
            version=meta.version,
            backend=type(store).__name__,
        )
        return plaintext

    async def rotate(
        self,
        secret_id: str,
        new_plaintext: str,
        expected_version: int,
    ) -> int:
        """原子轮换并返回新版本；旧版本立即不可用于新调用。

        失败时（``store.rotate`` 抛异常）旧值保留、版本不递增、metadata 不变，
        调用方可继续用旧 ``secret_id`` resolve 旧明文。
        """
        if not new_plaintext:
            raise ValueError("new_plaintext must not be empty")
        meta = self._require_active(secret_id)
        if meta.version != expected_version:
            raise VersionConflictError(
                f"expected version {expected_version}, current {meta.version}",
                context={
                    "secret_id": secret_id,
                    "expected": expected_version,
                    "current": meta.version,
                },
            )
        store = self._select_store()
        # 原子替换；失败抛异常，旧值保留，下面 version 不递增。
        await store.rotate(meta.backend_key, new_plaintext)
        meta.version += 1
        meta.last_rotated_at = _now()
        meta.fingerprint = _fingerprint(new_plaintext)
        self._audit(
            "secret.rotated",
            secret_id=secret_id,
            version=meta.version,
            owner_type=meta.owner_type,
            owner_id=meta.owner_id,
        )
        return meta.version

    async def delete(self, secret_id: str) -> None:
        """仅无有效引用时删除；幂等，审计保留。"""
        meta = self._metadata.get(secret_id)
        if meta is None:
            # 幂等：已删除视为成功。
            self._audit("secret.deleted", secret_id=secret_id, status="ALREADY_DELETED")
            return
        store = self._select_store()
        try:
            await store.revoke(meta.backend_key)
        finally:
            meta.status = _STATUS_REVOKED
            self._metadata.pop(secret_id, None)
        self._audit("secret.deleted", secret_id=secret_id)

    # ------------------------------------------------------------------ #
    # 测试与运维辅助
    # ------------------------------------------------------------------ #

    def get_metadata(self, secret_id: str) -> SecretMetadata | None:
        """返回 metadata 副本（不含明文）；不存在返回 ``None``。"""
        meta = self._metadata.get(secret_id)
        return meta

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _select_store(self) -> SecretStore:
        """Keyring 优先；primary 自报不可用且配置 fallback 时改用 fallback。"""
        if self._fallback is not None:
            avail = getattr(self._primary, "is_available", None)
            if callable(avail) and not avail():
                return self._fallback
        return self._primary

    def _require_active(self, secret_id: str) -> SecretMetadata:
        meta = self._metadata.get(secret_id)
        if meta is None:
            raise NotFoundError(
                "secret not found",
                context={"secret_id": secret_id},
            )
        if meta.status != _STATUS_ACTIVE:
            raise NotFoundError(
                f"secret {secret_id} is {meta.status}",
                context={"secret_id": secret_id, "status": meta.status},
            )
        return meta

    def _audit(self, event: str, **fields: Any) -> None:
        """写审计事件；经 redact_processor 确保明文不进日志。"""
        self._log.info(event, **fields)


__all__ = [
    "LocalSecretService",
    "PermissionPolicy",
    "SecretMetadata",
]
