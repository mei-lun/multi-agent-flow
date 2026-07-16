# Secret 接口逻辑

Secret 明文只能从用户创建/轮换请求进入 SecretService，并只在已授权 Gateway 调用前短暂解析。Server–Runner 契约、Artifact、SSE、审计和普通数据库字段都不得包含明文。

