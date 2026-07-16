# TASK-062 Git 事件唤醒 Scheduler
## 前置条件
- TASK-021、TASK-026、TASK-060、TASK-061 已完成。
## 任务内容
- 将已进入 control 的 Submission/Blocked/Decision 事件按 event_id 唤醒对应 Run。
## 验收标准
- [ ] 重复事件只唤醒一次。
- [ ] 非当前 task/epoch 不能恢复 Graph。
- [ ] 失败唤醒可重试并保留记录。
## 不包含
- Gate 决策。

