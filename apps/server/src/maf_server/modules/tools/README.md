# Tool 与 Policy 接口逻辑

Tool Definition 描述“有什么动作”；Role Binding 描述“某角色能否申请动作”；Capability Policy 描述“当前 Attempt 在哪些参数范围内能否执行”。三者必须同时成立。

```text
配置 Router → ToolConfigurationService → ToolRepository / MCP discovery
Runtime Tool 请求 → ToolGateway → CapabilityPolicyService
                                  → 参数约束
                                  → 必要时 Inbox Approval
                                  → Native/HTTP/MCP Adapter
                                  → ToolCall + Audit
```

策略模拟绝不能执行真实工具。Gateway 对策略异常、Schema 缺失和上下文缺失一律 fail-closed。

