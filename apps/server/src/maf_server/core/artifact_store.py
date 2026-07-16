"""大文件内容寻址存储接口。业务元数据由 ArtifactRepository 保存。"""

from typing import AsyncIterator, BinaryIO, Protocol


class StoredObject(dict):
    """至少包含 sha256、content_length、storage_key 和 created_at。"""


class ArtifactFileStore(Protocol):
    async def put_stream(self, stream: BinaryIO, expected_sha256: str, expected_length: int) -> StoredObject:
        """边读边计算哈希写临时文件，完全匹配后原子移动到内容地址。

        不能一次读入内存；长度/哈希不符删除临时文件；相同 hash 已存在时复用但仍核对大小。
        storage_key 是内部相对键，不能是调用者提供的绝对路径。
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

