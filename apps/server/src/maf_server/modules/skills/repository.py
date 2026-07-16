"""Skill 元数据和文件索引持久化接口。"""

from typing import Protocol
from .schemas import SkillVersionView, SkillView


class SkillRepository(Protocol):
    async def get_skill(self, skill_id: str) -> SkillView | None:
        """读取 Skill Definition；不存在为 None。"""
        ...
    async def get_version(self, version_id: str) -> SkillVersionView | None:
        """读取精确版本及扫描/测试状态，不自动选择 latest。"""
        ...
    async def save_version(self, version: SkillVersionView) -> SkillVersionView:
        """创建 DRAFT 或单向状态更新；PUBLISHED 内容哈希不可更改。"""
        ...
    async def find_file(self, version_id: str, normalized_path: str) -> dict | None:
        """从扫描生成的文件索引查相对路径，返回 storage key/hash/size。"""
        ...
    async def is_version_bound_to_attempt(self, version_id: str, attempt_id: str) -> bool:
        """查询不可变 Run Snapshot 的精确绑定；只有完全匹配返回 true。"""
        ...
