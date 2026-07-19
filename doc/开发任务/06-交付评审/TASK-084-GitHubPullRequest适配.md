# TASK-084 GitHub Pull Request 适配
## 前置条件
- TASK-029、TASK-035、TASK-083 已完成。
## 任务内容
- 实现按 run/task marker 幂等创建/查询 PR，刷新 head、checks、approval 和 mergeable。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-084` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [x] 重试不创建重复 PR。
- [x] PR head 变化会使旧评审失效。
- [x] GitHub Token 仅从 SecretStore 解析。
## 不包含
- 最终业务 Gate。
