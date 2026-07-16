# TASK-070 Git 任务工作区
## 前置条件
- TASK-012、TASK-026、TASK-068 已完成。
## 任务内容
- 从 control 记录的 base 创建任务 worktree/branch，校验分支名、commit/tree 和允许路径。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-070` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [ ] 不修改用户权威工作树。
- [ ] 分支严格为当前 task/epoch/node。
- [ ] 收集 head、tree 和 changed paths。
## 不包含
- push 和提交事件。
