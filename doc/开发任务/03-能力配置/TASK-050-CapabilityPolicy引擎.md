# TASK-050 Capability Policy 引擎
## 前置条件
- TASK-031、TASK-048 已完成。
## 任务内容
- 组合 PyCasbin 和路径、URL、网络、预算、参数验证器，输出约束后决策。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-050` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [x] Role、Git Task Grant、assignment epoch 和参数约束取交集。
- [x] 策略缺失/异常默认拒绝。
- [x] 模拟接口无副作用且返回 reason/obligations。
## 不包含
- Tool Adapter 执行。
