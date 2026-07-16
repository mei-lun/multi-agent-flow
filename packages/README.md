# Shared Packages

共享包只存放可复用契约和与框架无关的逻辑：

- `contracts_py`：server、runner、容器 Runtime 共用的 Pydantic DTO。
- `domain`：状态、实体和值对象，不依赖 FastAPI、SQLAlchemy、LangGraph 或 Docker。
- `artifact_schemas`：Artifact 的 Pydantic/JSON Schema。
- `provider_adapters`：模型 Provider Adapter 接口与实现。
- `tool_adapters`：原生、HTTP、MCP Tool Adapter。
- `repository_adapters`：GitHub 和本地 Git Adapter。
- `policy`：Casbin model、policy 和参数校验器。
- `observability`：结构化日志、trace ID 和事件辅助函数。
