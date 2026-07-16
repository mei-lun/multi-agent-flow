"""创建和管理独立 LangGraph SQLite Checkpointer 的接口。"""

from typing import Any


def create_checkpointer(database_path: str) -> Any:
    """创建 checkpointer 并应用 busy_timeout 等轻量配置。

    database_path 必须位于 Server 数据目录，且不能等于 maf.db。返回对象只交给 Scheduler；
    Runner 和业务 Router 不得访问。函数不迁移业务表。
    """
    ...
