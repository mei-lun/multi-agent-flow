# TASK-008 UnitOfWork 与乐观锁
## 前置条件
- TASK-006、TASK-007 已完成。
## 任务内容
- 实现异步 UnitOfWork、短写事务、提交/回滚和通用 expected_version 更新辅助函数。
## 验收标准
- [ ] 未 commit 或发生异常时自动回滚。
- [ ] 并发版本更新只有一个成功，另一个返回冲突。
- [ ] 网络和 Git 操作不会在写事务内执行。
## 不包含
- 具体 Repository SQL。

