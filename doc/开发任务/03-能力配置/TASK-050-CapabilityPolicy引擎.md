# TASK-050 Capability Policy 引擎
## 前置条件
- TASK-031、TASK-048 已完成。
## 任务内容
- 组合 PyCasbin 和路径、URL、网络、预算、参数验证器，输出约束后决策。
## 验收标准
- [ ] Role、Git Task Grant、assignment epoch 和参数约束取交集。
- [ ] 策略缺失/异常默认拒绝。
- [ ] 模拟接口无副作用且返回 reason/obligations。
## 不包含
- Tool Adapter 执行。

