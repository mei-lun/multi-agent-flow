"""Review 公共 HTTP 接口。"""

from typing import Protocol
from maf_contracts.common import ActorContext
from .schemas import ReviewPage, ReviewQuery


class ReviewHttpApi(Protocol):
    async def get_reviews(self, actor: ActorContext, query: ReviewQuery) -> ReviewPage:
        """GET `/api/v1/reviews`；按权限过滤的游标分页查询。"""
        ...

