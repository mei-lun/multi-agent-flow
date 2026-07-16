# Tool Gateway 接口与调用逻辑

```text
节点本地 Runtime → list allowed / call
  → ExecutionContext 中的 control commit / assignment epoch
  → Role Snapshot + Git Task Grant 精确版本
  → input JSON Schema
  → CapabilityPolicyService
  → [WAITING_APPROVAL → Inbox → 再校验]
  → Native / HTTP / MCP Adapter
  → output Schema + size limit
  → Artifact + ToolCall + Audit/Event
```

需要中央审批时节点提交 BLOCKED 事件并停止当前 Attempt；审批结果进入 control 后重新执行。审批后仍须校验 assignment epoch、主题版本、参数和策略。Adapter 不能自行读取 Secret 或扩大 URL/路径。
