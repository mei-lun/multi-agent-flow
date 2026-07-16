# TASK-084 GitHub Pull Request 适配
## 前置条件
- TASK-029、TASK-035、TASK-083 已完成。
## 任务内容
- 实现按 run/task marker 幂等创建/查询 PR，刷新 head、checks、approval 和 mergeable。
## 验收标准
- [ ] 重试不创建重复 PR。
- [ ] PR head 变化会使旧评审失效。
- [ ] GitHub Token 仅从 SecretStore 解析。
## 不包含
- 最终业务 Gate。

