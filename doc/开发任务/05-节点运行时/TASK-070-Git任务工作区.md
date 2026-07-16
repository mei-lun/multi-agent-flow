# TASK-070 Git 任务工作区
## 前置条件
- TASK-012、TASK-026、TASK-068 已完成。
## 任务内容
- 从 control 记录的 base 创建任务 worktree/branch，校验分支名、commit/tree 和允许路径。
## 验收标准
- [ ] 不修改用户权威工作树。
- [ ] 分支严格为当前 task/epoch/node。
- [ ] 收集 head、tree 和 changed paths。
## 不包含
- push 和提交事件。

