# TASK-036 项目变更请求
## 前置条件
- TASK-009、TASK-033、TASK-082 已完成。
## 任务内容
- 实现运行中需求变更记录、受影响需求、请求动作和站内审批创建。
## 验收标准
- [ ] 只能针对本项目非终态 Run 创建。
- [ ] 请求不直接修改 checkpoint 或 control task。
- [ ] 决策通过事件交给 Scheduler 处理。
## 不包含
- Scheduler 重规划实现。

