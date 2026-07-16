# Model Connection 接口与调用逻辑

配置层保存“去哪里调用、使用哪个模型、如何 fallback”；执行层 Model Gateway 才进行推理。

```text
配置 Router → ModelConnectionService → SecretService
                                      → ProviderAdapter（仅 verify/probe）
                                      → ModelConfigurationRepository

Runner → Internal Model API → ModelGateway → PolicyService
                                         → SecretService
                                         → ProviderAdapter
                                         → Usage/Audit/Artifact
```

Key 只在创建或轮换请求中出现一次。连接验证与模型能力探测分开：连接可用不代表模型支持 Tool 或 JSON Schema。Profile 能力必须以探测结果为准。运行中只引用已发布 Model Policy 的快照，不读取用户刚修改的默认配置。

