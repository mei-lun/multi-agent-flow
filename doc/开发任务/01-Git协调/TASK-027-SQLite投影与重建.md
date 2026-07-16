# TASK-027 SQLite 投影与重建
## 前置条件
- TASK-007～009、TASK-016、TASK-021～026 已完成。
## 任务内容
- 将 control tasks/nodes/events 投影到 SQLite，保存 projected_control_commit 并支持清空重建。
## 验收标准
- [ ] control push 确认后才推进投影水位。
- [ ] 中途失败可从旧水位重放。
- [ ] 重建结果与增量投影一致。
## 不包含
- Web 查询页面。

