"""数据库迁移包。

提供顺序 SQL 迁移执行器 ``MigrationRunner``，按 ``NNNN_description.sql``
命名规范扫描迁移脚本，应用并记录到 ``schema_migrations`` 表。

参见 ``migrations/README.md`` 与 ``doc/开发任务/00-基础/TASK-007-数据库迁移器.md``。
"""

from __future__ import annotations

from migrations.runner import Migration, MigrationError, MigrationRunner

__all__ = ["Migration", "MigrationError", "MigrationRunner"]
