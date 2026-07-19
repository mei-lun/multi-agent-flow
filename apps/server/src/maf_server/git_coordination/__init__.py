"""MAF Git coordination schema loading and validation.

本模块位于 ``apps/server/src/maf_server/git_coordination/``，负责加载
``templates/git_coordination/schemas/`` 下的 JSON Schema 文件，并对
``.maf/`` 中的 project.yaml、task、node、event 文件做结构校验。

与《GitHub 分布式协作协议》第 3 节权威目录对齐：
- ``project.yaml`` 用 ``project-v1`` 校验；
- ``tasks/<task-id>.yaml`` 用 ``task-v1`` 校验；
- ``nodes/<node-id>.yaml`` 用 ``node-v1`` 校验；
- ``events/<yyyy-mm>/<event-id>.json`` 用 ``event-v1`` 校验。
"""
