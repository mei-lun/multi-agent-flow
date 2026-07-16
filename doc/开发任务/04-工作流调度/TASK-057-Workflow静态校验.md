# TASK-057 Workflow 静态校验
## 前置条件
- TASK-053、TASK-056 已完成。
## 任务内容
- 校验 start、可达性、结束路径、环、Role、输入输出 Contract、受限条件和重试/返工上限。
## 验收标准
- [ ] 一次返回全部错误和警告。
- [ ] 条件表达式不能执行 Python/JavaScript。
- [ ] 不可达节点和无限返工阻断发布。
## 不包含
- Graph 运行。

