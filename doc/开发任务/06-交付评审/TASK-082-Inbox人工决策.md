# TASK-082 Inbox 人工决策
## 前置条件
- TASK-009、TASK-031、TASK-081 已完成。
## 任务内容
- 实现站内待办创建、查询、过期和按 subject version 的不可变决策。
## 验收标准
- [ ] 只有 assignee/管理员可决定。
- [ ] subject 版本变化返回冲突。
- [ ] Decision 与关闭 Item 原子提交并唤醒 Scheduler。
## 不包含
- 邮件或企业 IM。

