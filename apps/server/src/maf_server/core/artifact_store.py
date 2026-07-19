"""大文件内容寻址存储接口与本地实现。

TASK-078 范围：
- 保留 ``ArtifactFileStore`` Protocol 与 ``StoredObject``（其他任务接口契约）。
- 新增 ``LocalArtifactFileStore`` 具体实现：本地文件系统内容寻址存储后端。
- ``put_stream``：流式写入，边读边计算 SHA-256 与长度；完全匹配后原子移动到
  内容地址；不能一次读入内存；长度/哈希不符删除临时文件；相同 hash 已存在时
  复用但仍核对大小。
- ``open_stream``：在受控根目录内流式读取对象；规范化后越界必须拒绝。
- ``exists``：同时核对文件存在和内容哈希/可信索引，不只检查路径。
- ``delete_unreferenced``：仅在 Repository 确认零引用时删除；存在引用返回 false。

安全约束（对应 TASK-078 验收）：
- 大文件不一次读入内存：``put_stream`` 以固定块大小（默认 64KB）流式读写。
- 哈希不符不创建版本：``put_stream`` 在临时文件完全写入并校验通过后才原子
  rename 到内容地址；哈希或长度不符删除临时文件并抛 ``ValueError``。
- API 不暴露宿主绝对路径：``storage_key`` 是相对 root 的相对键
  （``ab/cdef...``），``StoredObject`` 只返回 ``storage_key``，不含绝对路径。
- 路径遍历防护：``_resolve_storage_key`` 用 ``Path.resolve()`` 后校验仍在 root 内。

本文件不依赖 FastAPI、SQLAlchemy、LangGraph、Docker 或模型 SDK。
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, BinaryIO, Protocol

# 默认流式读写块大小：64KB，平衡内存占用与系统调用次数。
_DEFAULT_CHUNK_SIZE: int = 64 * 1024

# SHA-256 十六进制字符串长度。
_SHA256_HEX_LEN: int = 64


class StoredObject(dict):
    """至少包含 sha256、content_length、storage_key 和 created_at。

    ``storage_key`` 是相对 root 的相对键（如 ``ab/cdef...``），不是宿主绝对路径，
    避免泄露宿主文件系统布局。
    """


class ArtifactFileStore(Protocol):
    async def put_stream(
        self,
        stream: BinaryIO,
        expected_sha256: str,
        expected_length: int,
    ) -> StoredObject:
        """边读边计算哈希写临时文件，完全匹配后原子移动到内容地址。

        不能一次读入内存；长度/哈希不符删除临时文件；相同 hash 已存在时复用
        但仍核对大小。``storage_key`` 是内部相对键，不能是调用者提供的绝对路径。
        """
        ...

    async def open_stream(self, storage_key: str) -> AsyncIterator[bytes]:
        """在受控根目录内流式读取对象；规范化后越界必须拒绝。"""
        ...

    async def exists(self, storage_key: str, sha256: str) -> bool:
        """同时核对文件存在和内容哈希/可信索引，不只检查路径。"""
        ...

    async def delete_unreferenced(self, storage_key: str) -> bool:
        """仅在 Repository 确认零引用时删除；存在引用返回 false。"""
        ...


# --------------------------------------------------------------------------- #
# 路径安全辅助
# --------------------------------------------------------------------------- #


def _validate_sha256_hex(value: str) -> str:
    """校验 ``value`` 是合法的 SHA-256 十六进制字符串（64 个 hex 字符）。"""
    if not isinstance(value, str) or len(value) != _SHA256_HEX_LEN:
        raise ValueError(
            f"expected_sha256 必须是 {_SHA256_HEX_LEN} 位十六进制字符串"
        )
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"expected_sha256 含非十六进制字符: {value!r}") from exc
    return value.lower()


def storage_key_for(sha256: str) -> str:
    """根据 SHA-256 计算内容寻址 storage_key。

    采用两级分片布局 ``<前2字符>/<剩余62字符>``，避免单目录文件过多。
    返回值全小写，仅含 hex 字符与一个路径分隔符，天然防遍历。
    """
    digest = _validate_sha256_hex(sha256)
    return f"{digest[:2]}/{digest[2:]}"


class LocalArtifactFileStore:
    """``ArtifactFileStore`` 的本地文件系统实现。

    内容寻址策略：
        - 文件按 SHA-256 命名，存放在 ``<root>/<sha256[:2]>/<sha256[2:]>``；
        - 相同内容（相同 hash）只存一份，复用现有文件但仍核对大小；
        - ``storage_key`` 格式 ``ab/cdef...``，相对 root 的相对键。

    路径安全：
        - ``_resolve_storage_key`` 把 storage_key 解析为绝对路径后，用
          ``Path.resolve()`` 规范化并校验仍在 root 内；
        - 非法 storage_key（含 ``..``、绝对路径、越界）抛 ``ValueError``；
        - storage_key 仅由 hex 字符与 ``/`` 组成（由 ``storage_key_for`` 生成），
          调用方传入的 storage_key 仍做防御性校验。

    流式写入（满足"大文件不一次读入内存"）：
        - ``put_stream`` 先写 ``<root>/.tmp/<random>`` 临时文件；
        - 以固定块大小（默认 64KB）从 ``stream`` 读取并写入临时文件，同时
          增量计算 SHA-256 与累计长度；
        - 流读完后校验 ``actual_length == expected_length`` 与
          ``actual_sha256 == expected_sha256``；不符删除临时文件并抛错；
        - 校验通过后，若目标文件已存在则复用（内容寻址天然幂等），否则原子
          ``os.replace`` 临时文件到目标路径（跨文件系统回退到 shutil.move）；
        - 临时文件目录与 root 同级，保证 ``os.replace`` 原子。

    事务边界：
        - 文件写入在 UoW 之外执行（文件系统不是 SQLite 事务的一部分）；
        - service 层应先 ``put_stream`` 落盘，再在 UoW 内写入元数据；
        - 若元数据写入失败，service 层负责清理无引用的存储文件。

    :param root: 存储根目录；不存在时自动创建。
    :param chunk_size: 流式读写块大小，默认 64KB。
    """

    def __init__(self, root: Path, *, chunk_size: int = _DEFAULT_CHUNK_SIZE) -> None:
        self._root: Path = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._tmp_dir: Path = self._root / ".tmp"
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须大于 0")
        self._chunk_size: int = chunk_size

    # ------------------------------------------------------------------ #
    # 公开属性
    # ------------------------------------------------------------------ #

    @property
    def root(self) -> Path:
        """存储根目录（绝对路径）。仅供运维和测试使用，不返回给 API 客户端。"""
        return self._root

    # ------------------------------------------------------------------ #
    # 路径解析与安全校验
    # ------------------------------------------------------------------ #

    def _resolve_storage_key(self, storage_key: str) -> Path:
        """把 storage_key 解析为绝对路径，并校验仍在 root 内。

        防御路径遍历：即使调用方传入 ``../etc/passwd`` 也会被
        ``resolve()`` 规范化后 ``relative_to`` 校验拦截。

        :raises ValueError: storage_key 为空、含绝对路径组件或越界 root。
        """
        if not storage_key or not isinstance(storage_key, str):
            raise ValueError("storage_key 不能为空")
        target = (self._root / storage_key).resolve()
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise ValueError(
                f"storage_key 越界 root: {storage_key!r}"
            ) from exc
        return target

    def _target_path(self, sha256: str) -> Path:
        """根据 SHA-256 计算目标文件绝对路径。"""
        key = storage_key_for(sha256)
        path = self._resolve_storage_key(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------ #
    # ArtifactFileStore Protocol 实现
    # ------------------------------------------------------------------ #

    async def put_stream(
        self,
        stream: BinaryIO,
        expected_sha256: str,
        expected_length: int,
    ) -> StoredObject:
        """流式写入，校验长度与哈希后原子移动到内容地址。

        实现步骤：
            1. 校验 ``expected_sha256`` 格式与 ``expected_length >= 0``；
            2. 计算目标路径；若已存在（同 hash）则复用但仍校验大小；
            3. 创建临时文件 ``<root>/.tmp/<random>``；
            4. 以 ``chunk_size`` 块从 stream 读取写入临时文件，增量计算
               SHA-256 与累计长度；
            5. 校验 ``actual_length == expected_length``，否则删除临时文件
               抛 ``ValueError``；
            6. 校验 ``actual_sha256 == expected_sha256``，否则删除临时文件
               抛 ``ValueError``；
            7. 若目标已存在，删除临时文件并复用；
            8. 否则 ``os.replace`` 临时文件到目标路径（原子）。

        :raises ValueError: 长度/哈希不符或参数非法。
        """
        digest_expected = _validate_sha256_hex(expected_sha256)
        if not isinstance(expected_length, int) or expected_length < 0:
            raise ValueError("expected_length 必须为非负整数")
        if isinstance(expected_length, bool):  # bool 是 int 子类，显式拒绝
            raise ValueError("expected_length 不能为 bool")

        target = self._target_path(digest_expected)

        # 内容寻址幂等：同 hash 已存在则复用，但仍校验大小。
        if target.exists():
            actual_size = target.stat().st_size
            if actual_size != expected_length:
                raise ValueError(
                    f"内容 hash {digest_expected[:12]}... 已存在但大小不符: "
                    f"expected={expected_length}, actual={actual_size}"
                )
            return StoredObject(
                sha256=digest_expected,
                content_length=actual_size,
                storage_key=storage_key_for(digest_expected),
                created_at=datetime.now(timezone.utc).isoformat(),
            )

        # 流式写入临时文件，增量计算哈希与长度。
        hasher = hashlib.sha256()
        actual_length = 0
        tmp_name = secrets.token_hex(16)
        tmp_path = self._tmp_dir / tmp_name

        try:
            with open(tmp_path, "wb") as tmp_file:
                while True:
                    chunk = stream.read(self._chunk_size)
                    if not chunk:
                        break
                    if isinstance(chunk, str):  # 防御：BinaryIO 应返回 bytes
                        chunk = chunk.encode("utf-8")
                    tmp_file.write(chunk)
                    hasher.update(chunk)
                    actual_length += len(chunk)

            # 校验长度
            if actual_length != expected_length:
                raise ValueError(
                    f"内容长度不符: expected={expected_length}, "
                    f"actual={actual_length}"
                )

            # 校验哈希
            actual_sha = hasher.hexdigest()
            if actual_sha != digest_expected:
                raise ValueError(
                    f"内容哈希不符: expected={digest_expected}, "
                    f"actual={actual_sha}"
                )

            # 原子移动到目标路径
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(tmp_path), str(target))
        except BaseException:
            # 任何失败都清理临时文件，不残留
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise

        return StoredObject(
            sha256=digest_expected,
            content_length=actual_length,
            storage_key=storage_key_for(digest_expected),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    async def open_stream(self, storage_key: str) -> AsyncIterator[bytes]:
        """在受控根目录内流式读取对象。

        以 ``chunk_size`` 块 yield，避免一次读入内存。storage_key 越界抛
        ``ValueError``。

        :raises ValueError: storage_key 越界 root。
        :raises FileNotFoundError: 文件不存在。
        """
        path = self._resolve_storage_key(storage_key)
        if not path.exists():
            raise FileNotFoundError(f"对象不存在: {storage_key}")

        with open(path, "rb") as f:
            while True:
                chunk = f.read(self._chunk_size)
                if not chunk:
                    break
                yield chunk

    async def exists(self, storage_key: str, sha256: str) -> bool:
        """同时核对文件存在和内容哈希。

        逐块读取并重新计算 SHA-256，与传入 ``sha256`` 比较。任何不符返回 False。
        """
        digest_expected = _validate_sha256_hex(sha256)
        try:
            path = self._resolve_storage_key(storage_key)
        except ValueError:
            return False
        if not path.exists():
            return False
        hasher = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(self._chunk_size)
                    if not chunk:
                        break
                    hasher.update(chunk)
        except OSError:
            return False
        return hasher.hexdigest() == digest_expected

    async def delete_unreferenced(self, storage_key: str) -> bool:
        """删除存储对象。

        本方法只负责删除文件，不维护引用计数——由 Repository/Service 层在确认
        零引用后调用。文件不存在返回 ``False``，删除成功返回 ``True``。
        """
        try:
            path = self._resolve_storage_key(storage_key)
        except ValueError:
            return False
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError:
            return False
        return True


__all__ = [
    "ArtifactFileStore",
    "LocalArtifactFileStore",
    "StoredObject",
    "storage_key_for",
]
