"""Skill 公共及内部 HTTP 接口。"""

from typing import BinaryIO, Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class SkillHttpApi(Protocol):
    async def post_import(self, actor: ActorContext, request: ImportSkillRequest, archive: BinaryIO) -> SkillVersionView:
        """POST `/api/v1/skills/import`；安全扫描后创建 DRAFT，成功 201。"""
        ...
    async def post_version(self, actor: ActorContext, skill_id: str, request: CreateSkillVersionRequest) -> SkillVersionView:
        """POST `/api/v1/skills/{id}/versions`；新版本成功 201。"""
        ...
    async def post_test(self, actor: ActorContext, version_id: str, request: TestSkillRequest) -> SkillTestResult:
        """POST `/api/v1/skill-versions/{id}/test`；异步测试可返回 202。"""
        ...
    async def post_publish(self, actor: ActorContext, version_id: str, request: PublishSkillRequest) -> SkillVersionView:
        """POST `/api/v1/skill-versions/{id}/publish`；发布成功 200。"""
        ...
