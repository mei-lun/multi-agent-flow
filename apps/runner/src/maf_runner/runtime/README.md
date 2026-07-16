# Agent Runtime 接口与逻辑

```text
maf/control Task Assignment + 本地 TaskDispatchEnvelope
  → ContextBuilder（Git 版本 + 精确 Role/Skill/Tool/Model 权限）
  → AgentLoop
      ├─ ModelClient → 节点本地 Model Gateway → 外部模型供应商
      ├─ ToolClient → 节点本地 Tool/Policy
      ├─ SkillClient → 已验证 Git 工作树
      └─ ProgressReporter
  → ArtifactPackager
  → AttemptResult
```

Runtime 是不可信执行环境的一部分。容器不能接触 Secret、SQLite、Docker Socket 或宿主机其他路径；节点宿主 Gateway 只按任务快照提供能力。所有循环必须有步数、Tool 次数、时间、token 和费用硬上限。需要中央人工决定时提交 BLOCKED 事件并结束当前执行，不在容器中阻塞。
