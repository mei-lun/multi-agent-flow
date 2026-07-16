# 业务模块约定

每个业务目录是一个独立边界，默认采用以下六个文件；只有确有职责时才增加文件：

- `domain.py`：实体、值对象、状态转换和业务不变量，不依赖 FastAPI、SQLite 或外部 SDK。
- `schemas.py`：HTTP 输入输出 DTO、查询条件和分页结构。
- `repository.py`：本模块的数据访问接口及 SQLite 实现，不跨模块拼装业务流程。
- `service.py`：用例编排、事务边界、权限检查和领域事件产生。
- `router.py`：FastAPI 路由、参数解析和响应映射，不承载业务规则。
- `events.py`：本模块发布或消费的事件负载定义。

模块间通过应用服务、稳定 ID 或领域事件协作，禁止直接读取其他模块的数据表。

`git_coordination` 是特殊基础模块：它没有节点 HTTP Router，以 Git refs/files 作为传输接口，并负责 control 单写与 SQLite 投影。其他子目录按上述标准实现。
