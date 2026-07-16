# TASK-060 LangGraph 编译与 Checkpoint
## 前置条件
- TASK-006、TASK-057～059 已完成。
## 任务内容
- 将发布 Workflow 编译为固定节点函数，使用 run_id thread key 和独立 SQLite Checkpointer。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-060` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [ ] 相同 workflow hash 编译等价图。
- [ ] Graph State 只存小字段和引用。
- [ ] 重启可从 checkpoint 恢复。
## 不包含
- 远程任务执行。
