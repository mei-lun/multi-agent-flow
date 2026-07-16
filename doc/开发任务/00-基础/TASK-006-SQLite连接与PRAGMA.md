# TASK-006 SQLite 连接与 PRAGMA
## 前置条件
- TASK-003、TASK-005 已完成。
## 任务内容
- 实现 `maf.db` 和 `checkpoints.db` 独立连接、WAL、foreign_keys、busy_timeout 和关闭流程。
## 验收标准
- [ ] 启动后 PRAGMA 值符合设计。
- [ ] 业务库与 checkpoint 库路径不同。
- [ ] 并发短写测试不产生未处理的 database locked。
## 不包含
- 业务表。

