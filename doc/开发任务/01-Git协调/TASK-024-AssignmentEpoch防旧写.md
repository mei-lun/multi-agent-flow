# TASK-024 Assignment Epoch 防旧写
## 前置条件
- TASK-022、TASK-023 已完成。
## 任务内容
- 实现 assignment_id、递增 epoch、based_on_control_commit 和旧事件 fencing 校验。
## 验收标准
- [ ] 重分配后旧 epoch 的进度和提交全部拒绝。
- [ ] 旧任务分支不会被删除。
- [ ] 当前 epoch 的合法事件正常处理。
## 不包含
- 超时判断。

