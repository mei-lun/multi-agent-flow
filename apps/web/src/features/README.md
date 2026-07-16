# 前端功能域约定

一个成熟功能域通常包含：

- `pages/`：路由页，只负责页面级数据装配。
- `components/`：该功能专属的展示与交互组件。
- `api.ts`：调用统一 HTTP client 的端点函数。
- `queries.ts`：查询、缓存键和刷新策略。
- `forms.ts`：表单模型、校验和服务端 DTO 转换。
- `types.ts`：只定义纯前端视图类型；服务端契约从 `api/contracts.ts` 引入。
- `index.ts`：功能域公开出口，禁止外部越层导入内部文件。

