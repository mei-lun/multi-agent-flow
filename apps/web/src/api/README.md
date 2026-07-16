# Web API 调用规则

`client.ts` 是所有 REST 请求的唯一 transport；`events.ts` 是 Run SSE 唯一入口；`contracts.ts` 只重新导出 OpenAPI 生成类型。各 feature 的 `api.ts` 可以组合端点函数，但不能绕过统一 client。

写请求生成并在重试时复用同一个 idempotency key；更新请求携带最新 expected_version；409 时提示用户刷新而不是自动覆盖。前端不得保存或回显 API Key，创建后只显示服务端提供的 configured/fingerprint 状态。

