# TASK-021 事件幂等与判定记录
## 前置条件
- TASK-008、TASK-011、TASK-019 已完成。
## 任务内容
- 按 event_id 保存接受/拒绝决定、reason_code 和 control commit，支持重复消费。
## 验收标准
- [ ] 同 event_id 相同内容返回首次决定。
- [ ] 同 event_id 不同内容被判冲突。
- [ ] 重启后不会重复应用状态变化。
## 不包含
- 具体事件业务规则。

