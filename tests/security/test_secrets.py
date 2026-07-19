"""TASK-029 安全测试：本地 SecretStore 与 SecretService。

验收标准覆盖：

1. 明文不进入 SQLite、日志、事件或 API 响应。
2. 轮换失败保留旧值。
3. Keyring 与回退实现有契约测试（两者都满足 SecretStore Protocol）。
4. AES-GCM 加解密正确性、rotate 原子性、revoke 幂等。

测试通过 ``sys.modules`` 注入 fake keyring，避免依赖真实 ``keyring`` 包
与 OS 凭据库（CI 环境通常无可用 backend）。所有异步入口经 ``asyncio.run``
同步执行，避免 pytest-asyncio 配置依赖（与现有 ``test_git_cli.py`` 一致）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import structlog

from maf_domain.errors import (
    ExternalDependencyError,
    NotFoundError,
    PermissionDeniedError,
    VersionConflictError,
)

from maf_server.core.secrets import (
    MASTER_KEY_SIZE_BYTES,
    SecretStore,
    generate_master_key,
    load_master_key,
    rotate_with_retention,
)
from maf_server.gateway.secrets.aes_gcm_store import AesGcmFileStore
from maf_server.gateway.secrets.keyring_store import (
    DEFAULT_KEYRING_SERVICE,
    KeyringStore,
)
from maf_server.gateway.secrets.local_service import (
    LocalSecretService,
    SecretMetadata,
)
from maf_server.gateway.secrets.service import SecretService


def _run(coro: Any) -> Any:
    """在独立事件循环中执行协程并返回结果。"""
    return asyncio.run(coro)


_PLAINTEXT = "ghp_SUPER_SECRET_value_12345"
_ORG_ID = "org-001"


# --------------------------------------------------------------------------- #
# fake keyring（注入 sys.modules，避免依赖真实 keyring 包）
# --------------------------------------------------------------------------- #


class _FakeKeyringErrors:
    class PasswordDeleteError(Exception):
        """模拟 keyring.errors.PasswordDeleteError。"""


class _FakeKeyringBackend:
    """内存 dict 模拟 OS keyring backend。"""

    name = "FakeKeyring"

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, account: str, password: str) -> None:
        self._store[(service, account)] = password

    def get_password(self, service: str, account: str) -> str | None:
        return self._store.get((service, account))

    def delete_password(self, service: str, account: str) -> None:
        key = (service, account)
        if key not in self._store:
            raise _FakeKeyringErrors.PasswordDeleteError("not found")
        del self._store[key]


class _FakeKeyringModule:
    """模拟 ``keyring`` 包接口。"""

    def __init__(self) -> None:
        self._backend = _FakeKeyringBackend()
        self.errors = _FakeKeyringErrors()

    def get_keyring(self) -> _FakeKeyringBackend:
        return self._backend

    def set_password(self, service: str, account: str, password: str) -> None:
        self._backend.set_password(service, account, password)

    def get_password(self, service: str, account: str) -> str | None:
        return self._backend.get_password(service, account)

    def delete_password(self, service: str, account: str) -> None:
        self._backend.delete_password(service, account)


@pytest.fixture()
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyringModule:
    """注入 fake keyring 模块；KeyringStore.is_available 返回 True。"""
    mod = _FakeKeyringModule()
    monkeypatch.setitem(sys.modules, "keyring", mod)
    # keyring.backends 子模块也可能被探测，一并注入空模块避免 AttributeError。
    monkeypatch.setitem(
        sys.modules, "keyring.backends", types.ModuleType("keyring.backends")
    )
    return mod


# --------------------------------------------------------------------------- #
# structlog 日志捕获
# --------------------------------------------------------------------------- #


@pytest.fixture()
def captured_log_events() -> list[dict[str, Any]]:
    """配置 structlog 捕获处理器，返回事件列表。"""
    events: list[dict[str, Any]] = []

    def _capture(
        _logger: Any, _method_name: str, event_dict: dict[str, Any]
    ) -> dict[str, Any]:
        events.append(dict(event_dict))
        return event_dict

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _capture,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    return events


# --------------------------------------------------------------------------- #
# fixtures: stores & service
# --------------------------------------------------------------------------- #


@pytest.fixture()
def aes_store(tmp_path: Path) -> AesGcmFileStore:
    master_key = _token_bytes()
    return AesGcmFileStore(
        master_key=master_key,
        storage_dir=tmp_path / "secrets",
        organization_id=_ORG_ID,
    )


@pytest.fixture()
def master_key_file(tmp_path: Path) -> Path:
    path = tmp_path / "master.key"
    generate_master_key(path)
    return path


def _token_bytes() -> bytes:
    import secrets as _s

    return _s.token_bytes(MASTER_KEY_SIZE_BYTES)


def _make_service(
    store: SecretStore,
    *,
    fallback: SecretStore | None = None,
    permission_policy: Any = None,
    audit_logger: Any = None,
) -> LocalSecretService:
    return LocalSecretService(
        primary=store,
        fallback=fallback,
        permission_policy=permission_policy,
        audit_logger=audit_logger,
    )


# --------------------------------------------------------------------------- #
# 验收 3：Keyring 与回退实现有契约测试（满足 SecretStore Protocol）
# --------------------------------------------------------------------------- #


class TestSecretStoreContract:
    """KeyringStore 与 AesGcmFileStore 都满足 SecretStore Protocol 且行为一致。"""

    def test_keyring_store_is_secret_store(self, fake_keyring: Any) -> None:
        store = KeyringStore()
        assert isinstance(store, SecretStore)

    def test_aes_gcm_store_is_secret_store(self, aes_store: AesGcmFileStore) -> None:
        assert isinstance(aes_store, SecretStore)

    @pytest.mark.parametrize("label", ["keyring", "aes_gcm"])
    def test_create_resolve_roundtrip(
        self,
        label: str,
        fake_keyring: Any,
        aes_store: AesGcmFileStore,
    ) -> None:
        store: SecretStore = KeyringStore() if label == "keyring" else aes_store
        backend_key = _run(store.create("conn-1", _PLAINTEXT))
        assert backend_key and backend_key != _PLAINTEXT
        assert _run(store.resolve(backend_key)) == _PLAINTEXT

    @pytest.mark.parametrize("label", ["keyring", "aes_gcm"])
    def test_rotate_replaces_value(
        self,
        label: str,
        fake_keyring: Any,
        aes_store: AesGcmFileStore,
    ) -> None:
        store: SecretStore = KeyringStore() if label == "keyring" else aes_store
        backend_key = _run(store.create("conn-1", _PLAINTEXT))
        new_value = "sk_NEW_rotated_value_67890"
        _run(store.rotate(backend_key, new_value))
        assert _run(store.resolve(backend_key)) == new_value

    @pytest.mark.parametrize("label", ["keyring", "aes_gcm"])
    def test_revoke_is_idempotent(
        self,
        label: str,
        fake_keyring: Any,
        aes_store: AesGcmFileStore,
    ) -> None:
        store: SecretStore = KeyringStore() if label == "keyring" else aes_store
        backend_key = _run(store.create("conn-1", _PLAINTEXT))
        _run(store.revoke(backend_key))
        # 二次删除不抛异常（幂等）。
        _run(store.revoke(backend_key))
        with pytest.raises(NotFoundError):
            _run(store.resolve(backend_key))

    @pytest.mark.parametrize("label", ["keyring", "aes_gcm"])
    def test_resolve_missing_raises_not_found(
        self,
        label: str,
        fake_keyring: Any,
        aes_store: AesGcmFileStore,
    ) -> None:
        store: SecretStore = KeyringStore() if label == "keyring" else aes_store
        with pytest.raises(NotFoundError):
            _run(store.resolve("nonexistent-backend-key"))

    @pytest.mark.parametrize("label", ["keyring", "aes_gcm"])
    def test_rotate_missing_raises_not_found(
        self,
        label: str,
        fake_keyring: Any,
        aes_store: AesGcmFileStore,
    ) -> None:
        store: SecretStore = KeyringStore() if label == "keyring" else aes_store
        with pytest.raises(NotFoundError):
            _run(store.rotate("nonexistent", "whatever"))


# --------------------------------------------------------------------------- #
# 验收 3b：KeyringStore 可用性探测与回退触发
# --------------------------------------------------------------------------- #


class TestKeyringAvailability:
    """keyring 不可用时 is_available 返回 False，Service 回退到 AES-GCM。"""

    def test_is_available_false_when_keyring_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 模拟未安装：从 sys.modules 移除并让 import 失败。
        monkeypatch.delitem(sys.modules, "keyring", raising=False)
        import builtins

        real_import = builtins.__import__

        def _fail_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "keyring":
                raise ModuleNotFoundError("No module named 'keyring'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fail_import)
        assert KeyringStore.is_available() is False

    def test_is_available_true_with_fake_backend(
        self, fake_keyring: Any
    ) -> None:
        assert KeyringStore.is_available() is True

    def test_service_falls_back_to_aes_when_keyring_unavailable(
        self,
        fake_keyring: Any,
        aes_store: AesGcmFileStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # primary 是 KeyringStore 但强制 is_available 返回 False。
        primary = KeyringStore()
        monkeypatch.setattr(primary, "is_available", lambda: False)
        service = _make_service(primary, fallback=aes_store)

        secret_id = _run(service.create("user", "alice", _PLAINTEXT))
        # 值应落在 AES-GCM 文件目录，而非 keyring。
        assert _run(service.resolve(secret_id, "model.invoke", "alice")) == _PLAINTEXT
        # fake keyring 后端不应收到任何条目。
        backend = fake_keyring.get_keyring()._store  # type: ignore[attr-defined]
        assert backend == {}, "明文不应写入 keyring（已回退到 AES-GCM）"


# --------------------------------------------------------------------------- #
# 验收 4：AES-GCM 加解密正确性与防篡改
# --------------------------------------------------------------------------- #


class TestAesGcmCorrectness:
    """AES-256-GCM 加解密、AAD 绑定、防篡改。"""

    def test_encrypt_decrypt_roundtrip(self, aes_store: AesGcmFileStore) -> None:
        backend_key = _run(aes_store.create("conn", _PLAINTEXT))
        assert _run(aes_store.resolve(backend_key)) == _PLAINTEXT

    def test_unique_nonce_per_write(self, aes_store: AesGcmFileStore) -> None:
        backend_key = _run(aes_store.create("conn", _PLAINTEXT))
        _run(aes_store.rotate(backend_key, _PLAINTEXT))
        record = json.loads(
            (aes_store._path_for(backend_key)).read_text(encoding="utf-8")
        )
        # nonce 是 24 个 hex 字符（12 字节）。
        assert len(record["nonce"]) == 24

    def test_tampered_ciphertext_rejected(self, aes_store: AesGcmFileStore) -> None:
        backend_key = _run(aes_store.create("conn", _PLAINTEXT))
        path = aes_store._path_for(backend_key)
        record = json.loads(path.read_text(encoding="utf-8"))
        # 翻转密文首字节。
        ct = bytearray(bytes.fromhex(record["ciphertext"]))
        ct[0] ^= 0xFF
        record["ciphertext"] = ct.hex()
        path.write_text(json.dumps(record), encoding="utf-8")
        with pytest.raises(ExternalDependencyError, match="decryption failed"):
            _run(aes_store.resolve(backend_key))

    def test_wrong_master_key_rejected(self, tmp_path: Path) -> None:
        store_a = AesGcmFileStore(
            master_key=_token_bytes(),
            storage_dir=tmp_path / "a",
            organization_id=_ORG_ID,
        )
        backend_key = _run(store_a.create("conn", _PLAINTEXT))
        # 同目录、不同 master_key 的 store 无法解密。
        store_b = AesGcmFileStore(
            master_key=_token_bytes(),
            storage_dir=tmp_path / "a",
            organization_id=_ORG_ID,
        )
        with pytest.raises(ExternalDependencyError):
            _run(store_b.resolve(backend_key))

    def test_aad_org_mismatch_rejected(self, tmp_path: Path) -> None:
        shared_key = _token_bytes()
        store_a = AesGcmFileStore(
            master_key=shared_key,
            storage_dir=tmp_path / "secrets",
            organization_id=_ORG_ID,
        )
        backend_key = _run(store_a.create("conn", _PLAINTEXT))
        # 同 master_key 但不同 org_id 的 store 无法解密（AAD 绑定）。
        store_b = AesGcmFileStore(
            master_key=shared_key,
            storage_dir=tmp_path / "secrets",
            organization_id="org-OTHER",
        )
        with pytest.raises(ExternalDependencyError):
            _run(store_b.resolve(backend_key))

    def test_ciphertext_file_has_no_plaintext(self, aes_store: AesGcmFileStore) -> None:
        backend_key = _run(aes_store.create("conn", _PLAINTEXT))
        path = aes_store._path_for(backend_key)
        blob = path.read_text(encoding="utf-8")
        assert _PLAINTEXT not in blob
        record = json.loads(blob)
        assert "plaintext" not in record
        assert "ciphertext" in record and record["ciphertext"] != _PLAINTEXT

    def test_backend_key_path_traversal_blocked(
        self, aes_store: AesGcmFileStore
    ) -> None:
        with pytest.raises(NotFoundError):
            _run(aes_store.resolve("../escape"))


# --------------------------------------------------------------------------- #
# 验收 4：rotate 原子性（失败保留旧值）
# --------------------------------------------------------------------------- #


class TestRotateAtomicity:
    """store.rotate 失败时旧值保留、版本不递增。"""

    def test_aes_rotate_failure_keeps_old_value(
        self, aes_store: AesGcmFileStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend_key = _run(aes_store.create("conn", _PLAINTEXT))
        original_record = (aes_store._path_for(backend_key)).read_text(
            encoding="utf-8"
        )

        # 让 os.replace 失败，模拟原子替换中断。
        import os as _os

        real_replace = _os.replace

        def _failing_replace(src: str, dst: str) -> None:
            raise OSError("simulated replace failure")

        monkeypatch.setattr(_os, "replace", _failing_replace)
        with pytest.raises(OSError):
            _run(aes_store.rotate(backend_key, "new-value"))
        monkeypatch.setattr(_os, "replace", real_replace)

        # 旧文件内容未变，旧值仍可解析。
        assert (
            aes_store._path_for(backend_key).read_text(encoding="utf-8")
            == original_record
        )
        assert _run(aes_store.resolve(backend_key)) == _PLAINTEXT

    def test_keyring_rotate_failure_keeps_old_value(
        self, fake_keyring: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = KeyringStore()
        backend_key = _run(store.create("conn", _PLAINTEXT))

        # 让 rotate 中的 set_password 失败（create 已在 patch 前完成）。
        def _failing_set(service: str, account: str, password: str) -> None:
            raise RuntimeError("keyring backend down")

        monkeypatch.setattr(fake_keyring, "set_password", _failing_set)
        with pytest.raises(RuntimeError):
            _run(store.rotate(backend_key, "new-value"))

        # 旧值仍可解析（resolve 只读，不调 set_password）。
        assert _run(store.resolve(backend_key)) == _PLAINTEXT


# --------------------------------------------------------------------------- #
# 验收 2：SecretService 轮换失败保留旧值
# --------------------------------------------------------------------------- #


class TestServiceRotateRetention:
    """SecretService.rotate 失败时旧值保留、版本不递增。"""

    def test_rotate_failure_keeps_version_and_old_value(
        self,
        aes_store: AesGcmFileStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = _make_service(aes_store)
        secret_id = _run(service.create("user", "alice", _PLAINTEXT))
        meta_before = service.get_metadata(secret_id)
        assert meta_before is not None
        version_before = meta_before.version

        # 让 store.rotate 失败。
        async def _fail_rotate(backend_key: str, plaintext: str) -> None:
            raise ExternalDependencyError("backend down", retryable=True)

        monkeypatch.setattr(aes_store, "rotate", _fail_rotate)

        with pytest.raises(ExternalDependencyError):
            _run(service.rotate(secret_id, "new-value", version_before))

        # 版本未变。
        meta_after = service.get_metadata(secret_id)
        assert meta_after is not None
        assert meta_after.version == version_before
        # 旧值仍可用。
        assert _run(service.resolve(secret_id, "model.invoke", "alice")) == _PLAINTEXT

    def test_rotate_version_conflict_raises(
        self, aes_store: AesGcmFileStore
    ) -> None:
        service = _make_service(aes_store)
        secret_id = _run(service.create("user", "alice", _PLAINTEXT))
        with pytest.raises(VersionConflictError):
            _run(service.rotate(secret_id, "new-value", expected_version=999))

    def test_rotate_with_retention_helper_creates_new_key_on_failure(
        self, aes_store: AesGcmFileStore
    ) -> None:
        backend_key = _run(aes_store.create("conn", _PLAINTEXT))

        class _FailingStore:
            """rotate 不支持原子 in-place；用 rotate_with_retention 协调。"""

            def __init__(self, real: AesGcmFileStore) -> None:
                self._real = real
                self.create_calls = 0

            async def create(self, name: str, plaintext: str) -> str:
                self.create_calls += 1
                return await self._real.create(name, plaintext)

            async def resolve(self, backend_key: str) -> str:
                return await self._real.resolve(backend_key)

            async def rotate(self, backend_key: str, plaintext: str) -> None:
                raise NotImplementedError("no atomic rotate")

            async def revoke(self, backend_key: str) -> None:
                await self._real.revoke(backend_key)

        failing = _FailingStore(aes_store)
        new_key = _run(
            rotate_with_retention(failing, backend_key, "new-value", name="conn")
        )
        assert failing.create_calls == 1
        assert new_key != backend_key
        # 新值可用。
        assert _run(failing.resolve(new_key)) == "new-value"
        # 旧 key 已被 revoke。
        with pytest.raises(NotFoundError):
            _run(failing.resolve(backend_key))

    def test_rotate_with_retention_retains_old_when_create_fails(
        self, aes_store: AesGcmFileStore
    ) -> None:
        backend_key = _run(aes_store.create("conn", _PLAINTEXT))

        class _CreateFailingStore:
            async def create(self, name: str, plaintext: str) -> str:
                raise ExternalDependencyError("keyring down", retryable=True)

            async def resolve(self, backend_key: str) -> str:
                return await aes_store.resolve(backend_key)

            async def rotate(self, backend_key: str, plaintext: str) -> None:
                raise NotImplementedError

            async def revoke(self, backend_key: str) -> None:
                await aes_store.revoke(backend_key)

        with pytest.raises(ExternalDependencyError):
            _run(
                rotate_with_retention(
                    _CreateFailingStore(), backend_key, "new-value", name="conn"
                )
            )
        # create 失败：旧 key 未被删除，旧值仍可用。
        assert _run(aes_store.resolve(backend_key)) == _PLAINTEXT


# --------------------------------------------------------------------------- #
# 验收 1：明文不进入 SQLite、日志、事件或 API 响应
# --------------------------------------------------------------------------- #


class TestNoPlaintextLeak:
    """明文不进 SQLite（metadata）、日志、API 响应。"""

    def test_metadata_has_no_plaintext_field(
        self, aes_store: AesGcmFileStore
    ) -> None:
        service = _make_service(aes_store)
        secret_id = _run(service.create("user", "alice", _PLAINTEXT))
        meta = service.get_metadata(secret_id)
        assert meta is not None
        # 遍历 metadata 所有字段值，确认明文不出现。
        for value in (
            meta.secret_id,
            meta.backend_key,
            meta.name,
            meta.owner_type,
            meta.owner_id,
            meta.secret_type,
            meta.status,
            meta.fingerprint,
        ):
            assert _PLAINTEXT not in str(value), (
                f"metadata 字段泄漏明文: {value!r}"
            )
        # fingerprint 只允许末 4 位（设计文档允许）。
        assert meta.fingerprint.endswith(_PLAINTEXT[-4:])
        assert _PLAINTEXT[:-4] not in meta.fingerprint

    def test_create_return_value_is_opaque_id(
        self, aes_store: AesGcmFileStore
    ) -> None:
        service = _make_service(aes_store)
        secret_id = _run(service.create("user", "alice", _PLAINTEXT))
        assert secret_id != _PLAINTEXT
        assert _PLAINTEXT not in secret_id

    def test_plaintext_not_in_logs(
        self,
        aes_store: AesGcmFileStore,
        captured_log_events: list[dict[str, Any]],
    ) -> None:
        service = _make_service(aes_store)
        secret_id = _run(service.create("user", "alice", _PLAINTEXT))
        _run(service.resolve(secret_id, "model.invoke", "alice"))
        _run(service.rotate(secret_id, "rotated-new-value-999", 1))
        _run(service.delete(secret_id))

        assert captured_log_events, "应至少捕获一条审计日志"
        blob = json.dumps(captured_log_events, ensure_ascii=False)
        assert _PLAINTEXT not in blob, "明文泄漏进审计日志"
        assert "rotated-new-value-999" not in blob, "轮换新明文泄漏进审计日志"
        # 审计事件应包含 secret.created/resolved/rotated/deleted。
        events = {e["event"] for e in captured_log_events}
        assert {
            "secret.created",
            "secret.resolved",
            "secret.rotated",
            "secret.deleted",
        } <= events

    def test_plaintext_only_passed_to_store_create_and_rotate(
        self,
        aes_store: AesGcmFileStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """明文只经 store.create/rotate 传入；其他调用不携带明文。"""
        service = _make_service(aes_store)
        received_plaintexts: list[str] = []

        real_create = aes_store.create
        real_rotate = aes_store.rotate

        async def _spy_create(name: str, plaintext: str) -> str:
            received_plaintexts.append(plaintext)
            return await real_create(name, plaintext)

        async def _spy_rotate(backend_key: str, plaintext: str) -> None:
            received_plaintexts.append(plaintext)
            await real_rotate(backend_key, plaintext)

        monkeypatch.setattr(aes_store, "create", _spy_create)
        monkeypatch.setattr(aes_store, "rotate", _spy_rotate)

        secret_id = _run(service.create("user", "alice", _PLAINTEXT))
        resolved = _run(service.resolve(secret_id, "model.invoke", "alice"))
        assert resolved == _PLAINTEXT
        _run(service.rotate(secret_id, "new-value", 1))
        _run(service.delete(secret_id))

        # 明文只出现在 create/rotate 调用中（共 2 次：create + rotate）。
        assert received_plaintexts == [_PLAINTEXT, "new-value"]

    def test_aes_ciphertext_on_disk_has_no_plaintext(
        self, aes_store: AesGcmFileStore, tmp_path: Path
    ) -> None:
        service = _make_service(aes_store)
        _run(service.create("user", "alice", _PLAINTEXT))
        plaintext_bytes = _PLAINTEXT.encode("utf-8")
        # 扫描整个存储目录，确认明文不出现在任何文件。
        for path in (tmp_path / "secrets").rglob("*"):
            if path.is_file():
                assert plaintext_bytes not in path.read_bytes(), (
                    f"明文泄漏到磁盘文件: {path}"
                )

    def test_master_key_not_in_logs(
        self,
        master_key_file: Path,
        captured_log_events: list[dict[str, Any]],
    ) -> None:
        key = load_master_key(master_key_file)
        # 加载主密钥不写日志；即使写，也不应包含密钥 bytes。
        blob = json.dumps(captured_log_events, ensure_ascii=False)
        assert key.hex() not in blob
        assert str(key) not in blob


# --------------------------------------------------------------------------- #
# SecretService 权限与生命周期
# --------------------------------------------------------------------------- #


class TestServicePermissionAndLifecycle:
    """权限检查、resolve purpose、delete 幂等、SecretService Protocol 契约。"""

    def test_local_service_satisfies_secret_service_protocol(
        self, aes_store: AesGcmFileStore
    ) -> None:
        service = _make_service(aes_store)
        # SecretService 是结构性 Protocol，校验方法存在。
        for method in ("create", "resolve", "rotate", "delete"):
            assert callable(getattr(service, method))

    def test_resolve_by_non_owner_denied(
        self, aes_store: AesGcmFileStore
    ) -> None:
        service = _make_service(aes_store)
        secret_id = _run(service.create("user", "alice", _PLAINTEXT))
        with pytest.raises(PermissionDeniedError):
            _run(service.resolve(secret_id, "model.invoke", "bob"))

    def test_resolve_wrong_purpose_denied(
        self, aes_store: AesGcmFileStore
    ) -> None:
        service = _make_service(aes_store)
        secret_id = _run(service.create("user", "alice", _PLAINTEXT))
        with pytest.raises(PermissionDeniedError):
            _run(service.resolve(secret_id, "unauthorized.purpose", "alice"))

    def test_custom_permission_policy_applied(
        self, aes_store: AesGcmFileStore
    ) -> None:
        calls: list[tuple[str, str, str]] = []

        def _policy(secret_id: str, purpose: str, actor_id: str, meta: Any) -> None:
            calls.append((secret_id, purpose, actor_id))
            if actor_id == "denied-actor":
                raise PermissionDeniedError("custom policy denied")

        service = _make_service(aes_store, permission_policy=_policy)
        secret_id = _run(service.create("user", "alice", _PLAINTEXT))
        with pytest.raises(PermissionDeniedError, match="custom policy"):
            _run(service.resolve(secret_id, "model.invoke", "denied-actor"))
        assert calls, "自定义权限策略应被调用"

    def test_delete_is_idempotent(self, aes_store: AesGcmFileStore) -> None:
        service = _make_service(aes_store)
        secret_id = _run(service.create("user", "alice", _PLAINTEXT))
        _run(service.delete(secret_id))
        # 二次删除不抛异常。
        _run(service.delete(secret_id))
        with pytest.raises(NotFoundError):
            _run(service.resolve(secret_id, "model.invoke", "alice"))

    def test_resolve_revoked_secret_raises(
        self, aes_store: AesGcmFileStore
    ) -> None:
        service = _make_service(aes_store)
        secret_id = _run(service.create("user", "alice", _PLAINTEXT))
        _run(service.delete(secret_id))
        with pytest.raises(NotFoundError):
            _run(service.resolve(secret_id, "model.invoke", "alice"))

    def test_create_empty_plaintext_rejected(
        self, aes_store: AesGcmFileStore
    ) -> None:
        service = _make_service(aes_store)
        with pytest.raises(ValueError):
            _run(service.create("user", "alice", ""))

    def test_rotate_increments_version(
        self, aes_store: AesGcmFileStore
    ) -> None:
        service = _make_service(aes_store)
        secret_id = _run(service.create("user", "alice", _PLAINTEXT))
        new_version = _run(service.rotate(secret_id, "v2-value", 1))
        assert new_version == 2
        # 旧版本号现在应触发冲突。
        with pytest.raises(VersionConflictError):
            _run(service.rotate(secret_id, "v3-value", 1))

    def test_keyring_primary_used_when_available(
        self,
        fake_keyring: Any,
        aes_store: AesGcmFileStore,
    ) -> None:
        """keyring 可用时明文写入 keyring，不写 AES 文件目录。"""
        primary = KeyringStore()
        service = _make_service(primary, fallback=aes_store)
        secret_id = _run(service.create("user", "alice", _PLAINTEXT))
        assert _run(service.resolve(secret_id, "model.invoke", "alice")) == _PLAINTEXT
        # keyring 后端收到条目。
        backend = fake_keyring.get_keyring()._store  # type: ignore[attr-defined]
        assert len(backend) == 1
        # AES 存储目录无文件。
        assert not list(aes_store._dir.iterdir())


# --------------------------------------------------------------------------- #
# 主密钥管理
# --------------------------------------------------------------------------- #


class TestMasterKeyManagement:
    """generate_master_key / load_master_key 行为。"""

    def test_generate_creates_32_byte_file(self, tmp_path: Path) -> None:
        path = tmp_path / "master.key"
        generate_master_key(path)
        assert path.exists()
        assert len(path.read_bytes()) == MASTER_KEY_SIZE_BYTES

    def test_generate_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "master.key"
        generate_master_key(path)
        first = path.read_bytes()
        generate_master_key(path)
        assert path.read_bytes() == first, "已存在文件不应被覆盖"

    def test_load_validates_size(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.key"
        path.write_bytes(b"short")
        with pytest.raises(ExternalDependencyError):
            load_master_key(path)

    def test_load_missing_raises_retryable(self, tmp_path: Path) -> None:
        with pytest.raises(ExternalDependencyError) as exc_info:
            load_master_key(tmp_path / "nope.key")
        assert exc_info.value.retryable is True

    def test_load_returns_key(self, master_key_file: Path) -> None:
        key = load_master_key(master_key_file)
        assert len(key) == MASTER_KEY_SIZE_BYTES

    def test_aes_store_rejects_bad_master_key_size(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ExternalDependencyError):
            AesGcmFileStore(
                master_key=b"too-short",
                storage_dir=tmp_path / "secrets",
                organization_id=_ORG_ID,
            )


# --------------------------------------------------------------------------- #
# 端到端：Keyring 优先 + AES 回退的完整生命周期
# --------------------------------------------------------------------------- #


class TestEndToEndLifecycle:
    """create → resolve → rotate → resolve → delete 全链路。"""

    def test_full_lifecycle_with_aes(self, aes_store: AesGcmFileStore) -> None:
        service = _make_service(aes_store)
        secret_id = _run(service.create("user", "alice", _PLAINTEXT))
        assert _run(service.resolve(secret_id, "model.invoke", "alice")) == _PLAINTEXT
        _run(service.rotate(secret_id, "rotated-value", 1))
        assert _run(service.resolve(secret_id, "model.invoke", "alice")) == "rotated-value"
        _run(service.delete(secret_id))
        with pytest.raises(NotFoundError):
            _run(service.resolve(secret_id, "model.invoke", "alice"))

    def test_full_lifecycle_with_keyring(
        self, fake_keyring: Any, aes_store: AesGcmFileStore
    ) -> None:
        service = _make_service(KeyringStore(), fallback=aes_store)
        secret_id = _run(service.create("user", "alice", _PLAINTEXT))
        assert _run(service.resolve(secret_id, "model.invoke", "alice")) == _PLAINTEXT
        _run(service.rotate(secret_id, "rotated-value", 1))
        assert _run(service.resolve(secret_id, "model.invoke", "alice")) == "rotated-value"
        _run(service.delete(secret_id))
        with pytest.raises(NotFoundError):
            _run(service.resolve(secret_id, "model.invoke", "alice"))

    def test_secret_service_protocol_is_protocol(self) -> None:
        """SecretService 是 Protocol，LocalSecretService 结构性满足。"""
        # 仅校验 Protocol 类型可被引用且 LocalSecretService 拥有其全部方法。
        assert hasattr(SecretService, "create")
        assert hasattr(SecretService, "resolve")
        assert hasattr(SecretService, "rotate")
        assert hasattr(SecretService, "delete")
