"""只在已授权调用点解析 Keyring/AES-GCM Secret 的接口。"""

from typing import Protocol


class SecretService(Protocol):
    async def create(self, owner_type: str, owner_id: str, plaintext: str) -> str:
        """把明文写入 OS Keyring；不可用时用主密钥 AES-GCM 加密，返回 opaque secret_id。

        plaintext 不得写数据库明文字段、日志、异常、事件或返回值。失败时不创建 owner 记录。
        """
        ...
    async def resolve(self, secret_id: str, purpose: str, actor_id: str) -> str:
        """检查 owner/purpose 权限后短暂返回明文给 Gateway；每次解析写审计但不写值。"""
        ...
    async def rotate(self, secret_id: str, new_plaintext: str, expected_version: int) -> int:
        """原子轮换并返回新版本；旧版本立即不可用于新调用。"""
        ...
    async def delete(self, secret_id: str) -> None:
        """仅无有效引用时删除；幂等，审计保留。"""
        ...
