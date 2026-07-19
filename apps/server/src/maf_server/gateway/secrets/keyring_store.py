"""OS Keyring 后端实现（优先）。

明文经 ``keyring.set_password`` 写入操作系统凭据库；SQLite/Git 只保存
opaque backend key（随机 token），永不保存明文。无可用 keyring backend
时由 ``LocalSecretService`` 回退到 ``AesGcmFileStore``。

``keyring`` 包采用延迟导入：模块导入不依赖 ``keyring``，运行时不可用则
``is_available`` 返回 ``False``、各方法抛 ``ExternalDependencyError``。
这样部署环境未安装 ``keyring`` 时 server 仍可启动并使用 AES-GCM 回退。
"""

from __future__ import annotations

from typing import Any

from maf_domain.errors import ExternalDependencyError, NotFoundError

from maf_server.core.secrets import SecretStore, new_backend_key

#: keyring service name（与设计文档 7.3 ``secrets.keyring_service`` 一致）。
DEFAULT_KEYRING_SERVICE = "multi-agent-flow"

#: 无可用 OS 凭据库的 backend 类名片段——这些 backend 会写明文文件或直接
#: 失败，必须排除，由上层回退到 AES-GCM。
_UNAVAILABLE_BACKEND_HINTS: tuple[str, ...] = (
    "fail.Keyring",
    "UnavailableKeyring",
    "null.Keyring",
    "fail",
    "null",
)


class KeyringStore:
    """使用 OS keyring 存取 Secret 明文。

    backend key 是与明文无关的随机 token，作为 keyring account 传入。
    明文只经 keyring API 传递，不进日志、数据库或异常文本。

    轮换原子性：``keyring.set_password`` 对同一 (service, account) 原子覆盖写，
    失败时旧值保留；``rotate`` 先校验存在再覆盖写，避免静默创建新条目。
    """

    def __init__(self, service: str = DEFAULT_KEYRING_SERVICE) -> None:
        self._service = service

    @staticmethod
    def is_available() -> bool:
        """检测 ``keyring`` 包是否可导入且当前 backend 非 fallback。

        返回 ``False`` 的情形：未安装 ``keyring``、``get_keyring`` 抛异常、
        或当前 backend 是 ``fail``/``null``/``Unavailable`` 等占位实现。
        """
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError:
            return False
        try:
            backend = keyring.get_keyring()
        except Exception:
            return False
        backend_name = f"{type(backend).__module__}.{type(backend).__name__}"
        lowered = backend_name.lower()
        for hint in _UNAVAILABLE_BACKEND_HINTS:
            if hint in lowered:
                return False
        return True

    def _require_keyring(self) -> Any:
        """延迟导入 ``keyring``；未安装时抛 ``ExternalDependencyError``。"""
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ExternalDependencyError(
                "keyring package not installed; use AesGcmFileStore fallback",
                retryable=False,
            ) from exc
        return keyring

    async def create(self, name: str, plaintext: str) -> str:
        """存储明文到 keyring，返回 opaque backend key。

        ``name`` 仅用于运维排查（当前不写入 keyring 元数据）；backend key
        是随机 token，与明文无关联。
        """
        keyring = self._require_keyring()
        backend_key = new_backend_key()
        keyring.set_password(self._service, backend_key, plaintext)
        return backend_key

    async def resolve(self, backend_key: str) -> str:
        """从 keyring 读取明文；不存在返回 ``NotFoundError``。"""
        keyring = self._require_keyring()
        value = keyring.get_password(self._service, backend_key)
        if value is None:
            raise NotFoundError(
                "secret not found in keyring",
                context={"backend_key": backend_key},
            )
        return value

    async def rotate(self, backend_key: str, plaintext: str) -> None:
        """原子覆盖写；先校验存在，避免静默创建新条目。失败时旧值保留。"""
        keyring = self._require_keyring()
        if keyring.get_password(self._service, backend_key) is None:
            raise NotFoundError(
                "cannot rotate missing secret",
                context={"backend_key": backend_key},
            )
        keyring.set_password(self._service, backend_key, plaintext)

    async def revoke(self, backend_key: str) -> None:
        """幂等删除；条目不存在视为成功。"""
        keyring = self._require_keyring()
        try:
            keyring.delete_password(self._service, backend_key)
        except Exception as exc:
            # keyring.errors.PasswordDeleteError 表示条目不存在，幂等成功。
            exc_name = type(exc).__name__
            if "PasswordDelete" in exc_name or "NotFound" in exc_name:
                return
            raise


__all__ = ["DEFAULT_KEYRING_SERVICE", "KeyringStore"]
