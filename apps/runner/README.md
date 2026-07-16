# maf-runner

Runner 是可独立部署的执行进程。它通过 HTTP 长轮询领取作业，在隔离工作区中执行 Agent 循环、Docker 命令和 Git 操作，再把进度与结果回传服务端。

Runner 不直接连接中心 SQLite，不决定工作流流向，也不持有未分配给当前作业的模型、技能或工具权限。

