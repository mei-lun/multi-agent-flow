# TASK-064 Run 暂停、恢复与取消
## 前置条件
- TASK-022、TASK-060～063 已完成。
## 任务内容
- 实现幂等 pause/resume/cancel 命令、合法状态转换和 control 任务取消/恢复信号。
## 验收标准
- [ ] 命令先持久化再推进 Scheduler。
- [ ] 活动节点通过 control 看到取消或新 epoch。
- [ ] 已终态 Run 不被改写。
## 不包含
- 强制回滚已发生 Git/Tool 副作用。

