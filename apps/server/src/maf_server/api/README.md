# API 层接口逻辑

`router.py` 组合面向 Web 的公共 `/api/v1` 和 health 路由；`dependencies.py` 从请求建立用户 Actor 身份并取得 Service；`errors.py` 把稳定领域错误映射为 HTTP 状态。跨机器节点不使用本 API。

Router 只做传输层工作：解析→认证上下文→调用 Service→映射响应。它不能打开事务、查询 Repository 或调用 Gateway。节点协作一律通过 Git 分支协议，禁止新增 `/internal` 节点路由。
