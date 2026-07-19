# TASK-038 Provider Adapter 基线
## 前置条件
- TASK-002、TASK-005、TASK-037 已完成。
## 任务内容
- 实现 LiteLLM 公共 Adapter、规范消息、响应、流、取消和错误映射。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-038` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [x] Codex/OpenAI兼容、GLM、DeepSeek、MiniMax、Kimi Code 可按配置路由。
- [x] Provider 错误统一为 code/category/retryable。
- [x] 异常和日志不含 Key。
## 不包含
- fallback 策略。
