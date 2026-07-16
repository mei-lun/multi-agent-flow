# TASK-083 受控应用任务分支
## 前置条件
- TASK-012、TASK-026、TASK-077、TASK-080 已完成。
## 任务内容
- 中央在受控 worktree 检查任务 head/base/tree，重放提交或合并到 integration branch。
## 验收标准
- [ ] 不直接信任节点工作区。
- [ ] base/head 不匹配进入冲突/返工。
- [ ] 操作幂等且不覆盖 main 未审内容。
## 不包含
- GitHub PR 创建。

