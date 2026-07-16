# TASK-029 本地 SecretStore
## 前置条件
- TASK-003～005 已完成。
## 任务内容
- 实现 OS Keyring 优先、AES-GCM 回退的 Secret 创建、解析、轮换和吊销。
## 验收标准
- [ ] 明文不进入 SQLite、日志、事件或 API 响应。
- [ ] 轮换失败保留旧值。
- [ ] Keyring 与回退实现有契约测试。
## 不包含
- Vault/KMS。

