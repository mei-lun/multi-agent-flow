# TASK-093 Run 与 Git 任务监控
## 前置条件
- TASK-027、TASK-059～065、TASK-088 已完成。
## 任务内容
- 实现 Run 图、时间线、control commit、Task/owner/epoch、进度、问题、分支和控制命令。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-093` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [x] SSE 断线可从 Last-Event-ID 续传。
- [x] 任务状态与 SQLite Git 投影一致。
- [x] Pause/Resume/Cancel 显示异步收敛状态。
## 不包含
- 节点日志流式上传。
