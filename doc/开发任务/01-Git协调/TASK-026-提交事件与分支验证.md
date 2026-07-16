# TASK-026 提交事件与分支验证
## 前置条件
- TASK-012、TASK-021、TASK-024 已完成。
## 任务内容
- 验证 Submission 的任务分支名、base/head 可达性、owner/epoch、修改清单和测试证据。
## 验收标准
- [ ] head 不存在、base 不符或分支越权时拒绝。
- [ ] 合法提交进入 SUBMITTED，不直接 DONE。
- [ ] 重复提交事件幂等。
## 不包含
- PR 和最终评审。

