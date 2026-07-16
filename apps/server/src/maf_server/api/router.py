"""顶层 Web API Router 注册。

公共业务路由使用 `/api/v1`。分布式节点没有 `/internal/v1` HTTP 路由，只通过 Git refs 和
`.maf` 文件契约参与协作。
"""

