# TASK-049 MCP 工具发现
## 前置条件
- TASK-029、TASK-048 已完成。
## 任务内容
- 连接已配置 MCP Server，发现 Tool/Schema 并幂等同步为 Tool Version。
## 验收标准
- [ ] 同步不执行远端 Tool。
- [ ] 远端缺失工具按策略禁用但不删除历史。
- [ ] 协议错误和 Secret 脱敏。
## 不包含
- MCP Tool 实际调用。

