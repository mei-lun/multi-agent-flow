# TASK-074 Agent Context Builder
## 前置条件
- TASK-042、TASK-047、TASK-050、TASK-068～073 已完成。
## 任务内容
- 从 control task、Role、Skill、Tool、Model 本地映射和输出 Contract 构造最小执行上下文。
## 验收标准
- [ ] 额外本机能力不会进入上下文。
- [ ] control commit、assignment epoch、base/hash 全部校验。
- [ ] 大输入只放索引/按需读取句柄。
## 不包含
- Agent 循环。

