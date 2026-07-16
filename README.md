# Multi Agent Flow

一个用于学习和技术试验的轻量多 Agent 协作平台。

项目采用 SQLite、LangGraph SQLite Checkpointer、GitHub control/node/task 分支协调、节点本地 ArtifactStore、LiteLLM/PyCasbin，以及独立 Docker Runner。跨机器禁止自建节点 HTTP，SQLite 是 Git 权威状态的可重建投影。

## 顶层目录

- `apps/server`：FastAPI 控制面、Scheduler 和内嵌 Gateway。
- `apps/runner`：通过 Git pull/push 协调的 Docker 任务执行节点。
- `apps/web`：React 管理控制台。
- `packages`：跨进程契约、领域类型和 Adapter 接口。
- `templates`：网站开发团队等可导入模板。
- `infra`：本地运行、Docker 和 SQLite 配置。
- `tests`：单元、契约、集成、安全和端到端测试。
- `scripts`：开发、初始化、备份和维护脚本。
- `doc`：调研、需求、分析、设计和框架文档。

详细职责见 [项目框架与目录职责说明](./doc/项目框架与目录职责说明.md)。

接口阶段文档：

- [接口总目录](./doc/接口总目录.md)：公共 API、内部 API 与非 HTTP 接口导航。
- [接口设计与实现规范](./doc/接口设计与实现规范.md)：字段、注释、错误、幂等、事务与实现步骤规则。
- [GitHub 分布式协作协议](./doc/GitHub分布式协作协议.md)：control 单写、节点事件、任务分支和 assignment fencing。
- [开发任务库](./doc/开发任务/README.md)：100 个可逐项实施和验收的小任务。
