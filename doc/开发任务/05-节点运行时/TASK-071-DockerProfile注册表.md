# TASK-071 Docker Profile 注册表
## 前置条件
- TASK-003、TASK-066 已完成。
## 任务内容
- 实现镜像 digest、CPU、内存、磁盘、pids、超时和挂载白名单解析。
## 验收标准
- [ ] 浮动 latest 和未知 Profile 拒绝。
- [ ] Task 只能收紧本地 Profile。
- [ ] Profile 校验有边界测试。
## 不包含
- 创建容器。

