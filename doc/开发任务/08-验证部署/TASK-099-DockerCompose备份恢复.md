# TASK-099 Docker Compose、备份与恢复
## 前置条件
- TASK-065、TASK-077、TASK-098 已完成。
## 任务内容
- 构建 Server/Web 与本地 Node 镜像、Compose、健康检查，以及 SQLite/配置/本地 Artifact 备份恢复。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-099` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [x] 一条命令启动单机演示环境。
- [x] Git control 可重新构建中央投影。
- [x] 备份恢复后未结束 Run 和待办可继续。
## 不包含
- Kubernetes 和多区域高可用。
