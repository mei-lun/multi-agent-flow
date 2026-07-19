# TASK-061 Graph 节点发布 Git 任务
## 前置条件
- TASK-017、TASK-059、TASK-060 已完成。
## 任务内容
- 将 READY Agent 节点转换为 CoordinationTask，写能力、依赖、base、输入和输出契约。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-061` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [x] graph node 重放不重复创建任务。
- [x] 不在 Graph 节点中调用远程 Agent。
- [x] checkpoint 进入 WAITING_GIT_TASK。
## 不包含
- 节点 Claim。
