# TASK-056 Workflow Graph 持久化
## 前置条件
- TASK-008、TASK-055 已完成。
## 任务内容
- 实现 Node/Edge DTO、完整替换保存、规范化排序、graph hash 和乐观锁。
## 验收标准
- [ ] 并发保存冲突不覆盖。
- [ ] 同一逻辑 Graph 产生相同 hash。
- [ ] 只有 DRAFT 可保存。
## 不包含
- 静态检查。

