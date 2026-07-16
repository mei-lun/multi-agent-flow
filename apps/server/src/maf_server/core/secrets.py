"""Secret 后端存储端口；业务层应优先调用 gateway.secrets.SecretService。"""

from typing import Protocol


class SecretStore(Protocol):
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

