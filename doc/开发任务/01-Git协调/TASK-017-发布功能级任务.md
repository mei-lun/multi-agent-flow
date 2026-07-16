# TASK-017 发布功能级任务
## 前置条件
- TASK-015、TASK-016、TASK-022 已完成。
## 任务内容
- 将 Scheduler 任务写成独立 YAML，校验依赖并以 expected control head fast-forward push。
## 验收标准
- [ ] 同一幂等键只生成一个 task_id。
- [ ] 依赖不存在或成环时拒绝。
- [ ] push 冲突不会 force push 或覆盖远端。
## 不包含
- 节点选择。

