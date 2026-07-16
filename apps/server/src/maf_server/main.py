"""FastAPI application entry point.

Responsibilities:
- create the HTTP application;
- install public and internal routers;
- start scheduler, Git projector/event consumer, assignment reconciler, outbox, and cleanup lifecycles;
- serve the built web application in packaged deployments.
"""

from __future__ import annotations


def create_app():
    """构造 FastAPI Application 的接口。

    实现时先调用 build_container，再安装错误/trace/auth 中间件，挂载 public/internal router，
    最后注册启动和关闭生命周期。此入口不包含任何业务逻辑，import 模块时不得连接数据库。
    """
    ...
