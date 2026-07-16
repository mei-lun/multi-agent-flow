# TASK-011 加载 MAF 协议与 Schema
## 前置条件
- TASK-001、TASK-002、TASK-005 已完成。
## 任务内容
- 实现 `.maf/project.yaml` 与 task/node/event JSON Schema 加载、版本检查和错误定位。
## 验收标准
- [ ] 合法模板通过；未知协议版本拒绝。
- [ ] 缺字段、额外字段和错误枚举返回文件路径与字段路径。
- [ ] Schema 校验有固定样例测试。
## 不包含
- Git fetch/push。

