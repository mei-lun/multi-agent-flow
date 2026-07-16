# TASK-065 启动恢复与一致性修复
## 前置条件
- TASK-027、TASK-060～064 已完成。
## 任务内容
- 启动时 fetch Git、重建/追平投影、处理未消费事件并恢复非终态 Run。
## 验收标准
- [ ] 正常等待节点提交的 Run 不被误判失败。
- [ ] control/投影/checkpoint 不一致可修复或明确报警。
- [ ] 重复启动不重复创建 task、review 或 PR。
## 不包含
- 多 active Scheduler。

