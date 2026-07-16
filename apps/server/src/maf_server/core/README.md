# Core 接口逻辑

Core 只提供所有模块需要的基础端口：SQLite 连接、UnitOfWork、ArtifactFileStore、SecretStore、EventPublisher、安全上下文和 Clock。它不知道 Role、Workflow 或 Run 的业务含义。

数据库写事务必须短；大文件先写临时区并校验，再在业务事务中登记；外部网络调用通常放在事务外，通过状态和 Outbox 保证可恢复。禁止为了方便把业务 Service 放进 Core。

