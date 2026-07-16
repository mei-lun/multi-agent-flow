# Role 接口与调用逻辑

Role Version 是一次 Agent 执行权限的唯一来源。它同时固定 Prompt、Model Policy、Skill Version、Tool Grant、Capability Policy、Network Policy、资源 Profile 和限额。

```text
创建 DRAFT → 引用存在性检查 → 权限闭包检查 → 可选 dry-run
           → 发布时重新校验 → 计算 content_hash → PUBLISHED
           → Run Snapshot 固定引用 → Runner Context Builder
```

管理员身份不能在运行时绕过 Role Version。Skill 声明但 Role 未绑定的 Tool 一律不可用；Role 绑定但 Job Grant 未下发的能力也不可用。

