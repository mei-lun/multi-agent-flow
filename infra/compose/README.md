# Compose

`docker compose -f infra/compose/docker-compose.yml up --build` 启动单机演示环境，包含 Server、Web 和一个本地 Runner。SQLite、checkpoint、配置和 Artifact 共用 `maf-data` 卷；没有额外数据库容器。

备份和恢复使用 `scripts/backup.ps1` 与 `scripts/restore.ps1`。恢复前应停止服务，恢复后重新运行 Server 健康检查；control Git 仍是事实源，可通过 Scheduler projection rebuild 重新生成 SQLite 投影。
