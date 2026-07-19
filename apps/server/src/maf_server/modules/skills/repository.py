"""Skill 元数据和文件索引持久化接口。"""

import copy
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


class InMemorySkillRepository:
    """Small deterministic repository used by the local capability plane."""

    def __init__(self) -> None:
        self.skills: dict[str, SkillView] = {}
        self.versions: dict[str, SkillVersionView] = {}
        self.files: dict[tuple[str, str], dict] = {}
        self.bindings: set[tuple[str, str]] = set()

    async def get_skill(self, skill_id: str) -> SkillView | None:
        value = self.skills.get(skill_id)
        return copy.deepcopy(value) if value else None

    async def get_version(self, version_id: str) -> SkillVersionView | None:
        value = self.versions.get(version_id)
        return copy.deepcopy(value) if value else None

    async def save_version(self, version: SkillVersionView) -> SkillVersionView:
        existing = self.versions.get(version["id"])
        if existing and existing["status"] == "PUBLISHED" and existing != version:
            raise ValueError("published skill versions are immutable")
        self.versions[version["id"]] = copy.deepcopy(version)
        return copy.deepcopy(version)

    async def find_file(self, version_id: str, normalized_path: str) -> dict | None:
        value = self.files.get((version_id, normalized_path))
        return copy.deepcopy(value) if value else None

    async def is_version_bound_to_attempt(self, version_id: str, attempt_id: str) -> bool:
        return (version_id, attempt_id) in self.bindings
