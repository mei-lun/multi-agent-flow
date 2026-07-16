# TASK-004 统一日志和关联 ID
## 前置条件
- TASK-002、TASK-003 已完成。
## 任务内容
- 建立结构化日志字段、trace ID、run/task/event/control commit 关联字段和脱敏处理器。
## 验收标准
- [ ] Server 和 Node 日志均为结构化格式。
- [ ] Key、Token、密码和宿主机敏感路径会被脱敏。
- [ ] 同一请求或 Git 事件可通过关联 ID 追踪。
## 不包含
- 独立监控平台。

