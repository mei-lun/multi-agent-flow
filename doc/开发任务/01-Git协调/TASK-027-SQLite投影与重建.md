# TASK-027 SQLite 投影与重建
## 前置条件
- TASK-007～009、TASK-016、TASK-021～026 已完成。
## 任务内容
- 将 control tasks/nodes/events 投影到 SQLite，保存 projected_control_commit 并支持清空重建。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-027` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [ ] control push 确认后才推进投影水位。
- [ ] 中途失败可从旧水位重放。
- [ ] 重建结果与增量投影一致。
## 不包含
- Web 查询页面。
