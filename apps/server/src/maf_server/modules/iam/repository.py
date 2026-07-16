"""IAM 持久化接口。实现必须位于同一 UnitOfWork 中。"""

from typing import Protocol

from .schemas import SettingView, UserPage, UserQuery, UserView


class IamRepository(Protocol):
    async def get_user_by_id(self, user_id: str) -> UserView | None:
        """按 ID 查用户；不存在返回 None，不抛 HTTP 异常。"""
        ...

    async def get_user_auth_record(self, username: str) -> dict | None:
        """返回仅供登录使用的密码哈希记录；调用者不得将其映射到 API。"""
        ...

    async def list_users(self, query: UserQuery) -> UserPage:
        """使用稳定排序和不透明游标查询用户。"""
        ...

    async def save_user(self, user: UserView, expected_version: int | None) -> UserView:
        """新增或乐观锁更新用户；冲突时不得覆盖较新数据。"""
        ...

    async def get_setting(self, key: str) -> SettingView | None:
        """按稳定 key 读取非敏感设置视图；未知 key 返回 None。"""
        ...

    async def save_setting(self, setting: SettingView, expected_version: int | None) -> SettingView:
        """创建或乐观锁更新设置并返回新版本；不得保存 Secret 明文。"""
        ...
