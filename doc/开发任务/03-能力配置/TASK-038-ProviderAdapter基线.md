# TASK-038 Provider Adapter 基线
## 前置条件
- TASK-002、TASK-005、TASK-037 已完成。
## 任务内容
- 实现 LiteLLM 公共 Adapter、规范消息、响应、流、取消和错误映射。
## 验收标准
- [ ] Codex/OpenAI兼容、GLM、DeepSeek、MiniMax、Kimi Code 可按配置路由。
- [ ] Provider 错误统一为 code/category/retryable。
- [ ] 异常和日志不含 Key。
## 不包含
- fallback 策略。

