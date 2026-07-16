# TASK-051 节点本地 Tool Gateway
## 前置条件
- TASK-048～050、TASK-029 已完成。
## 任务内容
- 实现本地 Native/HTTP/MCP 调用、输入约束、超时、输出校验、取消和审计。
## 验收标准
- [ ] 容器看不到未授权 Tool 和 Secret。
- [ ] HTTP Tool 阻止私网跳转、超限响应和非法方法。
- [ ] 需中央审批时报告 BLOCKED，不跨节点等待 HTTP。
## 不包含
- Inbox 决策。

