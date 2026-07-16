"""Web 用户身份解析接口；跨节点身份由 Git 提交和节点清单验证。"""

from typing import Protocol
from maf_contracts.common import ActorContext


class IdentityService(Protocol):
    async def authenticate_session(self, session_token: str) -> ActorContext:
        """验证签名/会话/用户状态并构造服务端权限上下文；失败不返回部分身份。"""
        ...
