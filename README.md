# Multi Agent Flow

一个用于学习和技术试验的轻量多 Agent 协作平台。

项目采用 SQLite、LangGraph SQLite Checkpointer、本地 ArtifactStore、内嵌 LiteLLM/PyCasbin，以及独立 Docker Runner。当前阶段只建立模块边界、接口位置和文件职责，不实现完整业务逻辑。

## 顶层目录

- `apps/server`：FastAPI 控制面、Scheduler 和内嵌 Gateway。
- `apps/runner`：Docker 任务执行器。
- `apps/web`：React 管理控制台。
- `packages`：跨进程契约、领域类型和 Adapter 接口。
- `templates`：网站开发团队等可导入模板。
- `infra`：本地运行、Docker 和 SQLite 配置。
- `tests`：单元、契约、集成、安全和端到端测试。
- `scripts`：开发、初始化、备份和维护脚本。
- `doc`：调研、需求、分析、设计和框架文档。

详细职责见 [项目框架与目录职责说明](./doc/项目框架与目录职责说明.md)。
