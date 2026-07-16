# TASK-096 契约与 Git 并发测试
## 前置条件
- TASK-011～028、TASK-010 已完成。
## 任务内容
- 覆盖 task/node/event Schema、两节点同时 Claim、重复事件、push 冲突和旧 epoch 提交。
## 验收标准
- [ ] 并发 Claim 始终只有一个当前 owner。
- [ ] SQLite 投影可重建且结果一致。
- [ ] 所有拒绝具有稳定 reason_code。
## 不包含
- 浏览器 E2E。

