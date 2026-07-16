# Workspace 接口逻辑

Generic Workspace 用于文档任务；Git Workspace 用于代码任务。所有输入先校验 Artifact 哈希，所有路径先规范化。Runner 只产生 Patch/bundle/tree 信息，Server Repository Gateway 负责重新应用和 push。

