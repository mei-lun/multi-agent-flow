# TASK-075 有界 Agent Loop
## 前置条件
- TASK-043、TASK-051、TASK-074 已完成。
## 任务内容
- 实现 observe-think-act、模型调用、Tool 调用、上下文裁剪、最终输出和取消检查。
## 验收标准
- [ ] 步数、Tool 次数、时间、token 和费用上限生效。
- [ ] 未授权 Tool 不进入模型 Schema。
- [ ] 无无限自动重试；输出先按 Contract 预校验。
## 不包含
- 中央质量 Gate。

