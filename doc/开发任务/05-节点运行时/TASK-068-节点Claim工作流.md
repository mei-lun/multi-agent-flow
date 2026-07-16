# TASK-068 节点 Claim 工作流
## 前置条件
- TASK-023、TASK-024、TASK-067 已完成。
## 任务内容
- 选择能力匹配任务、提交 Claim、等待 control 接受/拒绝，并构造本地执行记录。
## 验收标准
- [ ] 未看到 owner/epoch 确认前不执行。
- [ ] 被其他节点取得时安全放弃。
- [ ] 本地并发不超过 capacity。
## 不包含
- Docker 执行。

