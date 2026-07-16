# Model Gateway 接口与调用逻辑

```text
节点 Agent Runtime → ExecutionContext + UnifiedModelRequest
  → control assignment / role snapshot / model policy 校验
  → call_key 幂等查询
  → budget reserve
  → primary profile → Policy → Secret resolve → Adapter invoke
  → 仅可重试错误才进入 fallback
  → response/schema 校验 → usage commit → audit/event
```

容器永远拿不到 Key；节点宿主本地 Gateway 只在调用前解析本地 Secret。Provider Adapter 只处理协议差异，不能决定角色权限或 fallback。Gateway 不信任 Runtime 提交的模型 ID、价格和 usage，必须以本地已验证配置及供应商响应为准。
