# TASK-080 确定性 Validator 框架
## 前置条件
- TASK-010、TASK-079 已完成。
## 任务内容
- 实现 supports/validate Registry，接入构建、测试、Schema、结构和安全扫描 Validator。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-080` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [x] ERROR 不被视为 PASS。
- [x] 每个结果包含阻断项、警告和证据引用。
- [x] Validator 可按 Artifact/任务类型选择。
## 不包含
- Gate 汇总。
