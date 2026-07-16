# TASK-093 Run 与 Git 任务监控
## 前置条件
- TASK-027、TASK-059～065、TASK-088 已完成。
## 任务内容
- 实现 Run 图、时间线、control commit、Task/owner/epoch、进度、问题、分支和控制命令。
## 验收标准
- [ ] SSE 断线可从 Last-Event-ID 续传。
- [ ] 任务状态与 SQLite Git 投影一致。
- [ ] Pause/Resume/Cancel 显示异步收敛状态。
## 不包含
- 节点日志流式上传。

