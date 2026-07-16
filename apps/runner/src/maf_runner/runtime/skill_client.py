"""只从已验证 Git 工作树读取 Role Snapshot 显式授权 Skill Version 的接口。"""

from typing import Protocol


class SkillClient(Protocol):
    async def read_file(self, skill_version_id: str, relative_path: str) -> bytes:
        """在仓库 Skill 索引中确认精确版本/hash，规范化路径并限制大小；不得访问仓库外路径。"""
        ...
