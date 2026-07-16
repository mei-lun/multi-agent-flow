# Compose

后续的 `docker-compose.yml` 只编排 `server` 与一个默认 `runner`，并挂载 `data/`。Web 构建产物由 Server 提供，不为 SQLite 增加数据库容器。

