"""仓库绑定状态和 RepositoryChange 投影持久化接口。"""

from typing import Protocol
from .schemas import RepositoryChangeView, RepositoryHealth


class RepositoryStateRepository(Protocol):
    async def get_binding_record(self, binding_id: str) -> dict | None:
        """返回 Gateway 所需位置/base/secret reference；不存在为 None。"""
        ...
    async def save_health(self, binding_id: str, health: RepositoryHealth) -> RepositoryHealth:
        """更新最近验证状态和固定 base commit，不写远端响应原文。"""
        ...
    async def get_change(self, change_id: str) -> RepositoryChangeView | None:
        """按 change ID 返回 PR/本地 Review 投影。"""
        ...
    async def get_change_by_run(self, run_id: str) -> RepositoryChangeView | None:
        """返回 Run 唯一 RepositoryChange；无代码仓库 Run 返回 None。"""
        ...
    async def save_change(self, item: RepositoryChangeView, expected_version: int | None) -> RepositoryChangeView:
        """乐观锁保存外部状态投影；不执行任何 Git/GitHub 动作。"""
        ...
