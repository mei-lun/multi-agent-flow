# Applications

运行时应用集合。依赖方向固定为：应用可以依赖 `packages`，`packages` 不得反向依赖 `apps`。

- `server`：唯一业务写入者，管理 SQLite、LangGraph、Artifact、模型、工具和 Git。
- `runner`：只通过内部 HTTP API 与 server 协作，不直接读取 SQLite 和 Secret。
- `web`：只调用公开 `/api/v1` 和 SSE，不调用 Runner 内部接口。
