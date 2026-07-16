# TASK-097 Scheduler 回放与故障注入
## 前置条件
- TASK-059～065、TASK-096 已完成。
## 任务内容
- 记录并回放 Git events、Gate 和人工决定，注入 control push 后 DB 失败、重启、重复唤醒和节点失联。
## 验收标准
- [ ] 相同历史得到相同最终状态。
- [ ] push 成功但投影失败可自动追平。
- [ ] 不重复创建任务、Attempt、Review 或 PR。
## 不包含
- 性能压测。

