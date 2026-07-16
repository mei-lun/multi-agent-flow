# maf-runner

Runner 是可独立部署的执行进程。它通过 Git fetch/push 读取 `maf/control`、提交节点事件和任务分支，在隔离工作区中执行 Agent 循环、Docker 命令和 Git 操作。

Runner 不连接中央 SQLite，不直接写 `maf/control`，不决定工作流流向，也不使用未分配给当前任务的模型、技能或工具权限。模型 Key 只保存在节点本地 SecretStore。
