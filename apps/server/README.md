# MAF Server

`maf-server` 是中央 SQLite 和 `maf/control` 的唯一逻辑写入者，负责公开 API、LangGraph 调度、Git 事件消费/投影、站内待办和最终 GitHub PR 门禁。跨机器节点不调用 Server HTTP。

启动入口为 `src/maf_server/main.py`，组装入口为 `bootstrap.py`。业务模块不得在 import 阶段启动后台任务或打开数据库。
