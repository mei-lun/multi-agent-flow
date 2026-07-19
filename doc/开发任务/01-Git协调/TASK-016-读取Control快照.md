# TASK-016 读取 control 快照
## 前置条件
- TASK-011、TASK-012、TASK-015 已完成。
## 任务内容
- fetch control，验证提交可达性和全部 task/node 文件，构造 `CoordinationSnapshot`。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-016` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [x] 快照包含 control commit、任务和节点。
- [x] 非 fast-forward 或 Schema 错误时不返回部分快照。
- [x] 读取不改变工作区文件。
## 不包含
- SQLite 投影。
