# TASK-061 Graph 节点发布 Git 任务
## 前置条件
- TASK-017、TASK-059、TASK-060 已完成。
## 任务内容
- 将 READY Agent 节点转换为 CoordinationTask，写能力、依赖、base、输入和输出契约。
## 验收标准
- [ ] graph node 重放不重复创建任务。
- [ ] 不在 Graph 节点中调用远程 Agent。
- [ ] checkpoint 进入 WAITING_GIT_TASK。
## 不包含
- 节点 Claim。

