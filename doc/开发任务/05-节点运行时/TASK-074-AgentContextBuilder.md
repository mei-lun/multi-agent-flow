# TASK-074 Agent Context Builder
## 前置条件
- TASK-042、TASK-047、TASK-050、TASK-068～073 已完成。
## 任务内容
- 从 control task、Role、Skill、Tool、Model 本地映射和输出 Contract 构造最小执行上下文。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-074` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [x] 额外本机能力不会进入上下文。
- [x] control commit、assignment epoch、base/hash 全部校验。
- [x] 大输入只放索引/按需读取句柄。
## 不包含
- Agent 循环。
