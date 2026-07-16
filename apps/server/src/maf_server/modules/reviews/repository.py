"""Review 和 Gate Decision 持久化接口。"""

from typing import Protocol
from .schemas import *


class ReviewRepository(Protocol):
    async def list_reviews(self, query: ReviewQuery, visible_project_ids: set[str]) -> ReviewPage:
        """在可见项目内按 run/type/status 过滤并游标分页。"""
        ...
    async def get_many(self, review_ids: list[str]) -> list[ReviewView]:
        """批量读取并保持输入 ID 顺序；缺失项不静默忽略，应由 Service 判为 Gate 材料缺失。"""
        ...
    async def save(self, review: ReviewView) -> ReviewView:
        """保存一次评审事实和证据引用；完成后正文不可覆盖。"""
        ...
    async def save_gate_decision(self, decision: GateDecisionView) -> GateDecisionView:
        """按 run+gate+输入版本哈希幂等保存确定性决策。"""
        ...
