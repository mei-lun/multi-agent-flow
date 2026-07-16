# Infrastructure

当前只维护轻量本地运行需要的配置：

- `compose`：可选的 server/runner Docker Compose。
- `docker`：Agent Runtime 镜像和 Docker Profile。
- `sqlite`：SQLite PRAGMA、初始化和备份说明。

PostgreSQL、对象存储和集群部署不属于当前 MVP。
