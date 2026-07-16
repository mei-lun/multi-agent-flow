"""Role Definition 与 Version 持久化接口。"""

from typing import Protocol
from .schemas import RoleVersionView, RoleView


class RoleRepository(Protocol):
    async def get_role(self, role_id: str) -> RoleView | None:
        """读取稳定 Definition；不存在为 None。"""
        ...
    async def get_version(self, version_id: str) -> RoleVersionView | None:
        """读取精确不可变版本；不得自动替换为 latest。"""
        ...
    async def save_role(self, role: RoleView) -> RoleView:
        """按组织唯一 key 创建/更新 Definition 元数据。"""
        ...
    async def save_version(self, version: RoleVersionView, expected_version: int | None = None) -> RoleVersionView:
        """创建 DRAFT 或乐观锁发布；PUBLISHED 字段禁止更新。"""
        ...
