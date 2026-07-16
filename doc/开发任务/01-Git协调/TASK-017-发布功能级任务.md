# TASK-017 发布功能级任务
## 前置条件
- TASK-015、TASK-016、TASK-022 已完成。
## 任务内容
- 将 Scheduler 任务写成独立 YAML，校验依赖并以 expected control head fast-forward push。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-017` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [ ] 同一幂等键只生成一个 task_id。
- [ ] 依赖不存在或成环时拒绝。
- [ ] push 冲突不会 force push 或覆盖远端。
## 不包含
- 节点选择。
