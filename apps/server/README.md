# MAF Server

`maf-server` 是唯一业务写入进程，负责公开 API、LangGraph 调度、SQLite、ArtifactStore、站内待办，以及模型、Tool、Git Gateway。

启动入口为 `src/maf_server/main.py`，组装入口为 `bootstrap.py`。业务模块不得在 import 阶段启动后台任务或打开数据库。
