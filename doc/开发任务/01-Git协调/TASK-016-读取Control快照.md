# TASK-016 读取 control 快照
## 前置条件
- TASK-011、TASK-012、TASK-015 已完成。
## 任务内容
- fetch control，验证提交可达性和全部 task/node 文件，构造 `CoordinationSnapshot`。
## 验收标准
- [ ] 快照包含 control commit、任务和节点。
- [ ] 非 fast-forward 或 Schema 错误时不返回部分快照。
- [ ] 读取不改变工作区文件。
## 不包含
- SQLite 投影。

