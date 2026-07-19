# TASK-006 SQLite 连接与 PRAGMA
## 前置条件
- TASK-003、TASK-005 已完成。
## 任务内容
- 实现 `maf.db` 和 `checkpoints.db` 独立连接、WAL、foreign_keys、busy_timeout 和关闭流程。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-006` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [x] 启动后 PRAGMA 值符合设计。
- [x] 业务库与 checkpoint 库路径不同。
- [x] 并发短写测试不产生未处理的 database locked。
## 不包含
- 业务表。
