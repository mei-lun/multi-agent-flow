# Applications

运行时应用集合。依赖方向固定为：应用可以依赖 `packages`，`packages` 不得反向依赖 `apps`。

- `server`：中央 SQLite 投影和 `maf/control` 单写者，管理 LangGraph、站内流程和 GitHub 门禁。
- `runner`：只通过 Git pull/push 参与跨机器协调；使用节点本地 SQLite、模型、工具和 Secret。
- `web`：只调用公开 `/api/v1` 和 SSE；不直接控制分布式节点。
