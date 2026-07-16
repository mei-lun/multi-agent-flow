# SQLite

`pragmas.sql` 是每个业务数据库连接必须应用的基线。业务数据库 `maf.db` 与 LangGraph 的 `checkpoints.db` 分离；只有 Server 进程可写这两个数据库。

