"""Composition root for server dependencies.

Only this module may wire concrete repositories, adapters, scheduler services,
and background workers to their Protocol interfaces.
"""

from __future__ import annotations


class ServerContainer:
    """Typed holder for long-lived server dependencies."""


def build_container():
    """创建并连接数据库、Store、Adapter、Gateway 和应用服务。

    顺序应为 config→database/store→repositories→gateway→services→scheduler→routers/workers。
    只有本文件可以选择具体实现；业务模块只依赖 Protocol。构造失败时按逆序关闭已创建资源。
    """
    ...
