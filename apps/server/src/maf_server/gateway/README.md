# Gateway 总体调用规则

Gateway 是外部副作用的唯一 Server 边界。Model、Tool、Repository、External Reuse 在执行前都依次完成身份、快照授权、Capability Policy、幂等和审计检查；需要 Secret 时只在实际调用前解析。

业务 Service 决定“为什么调用”和业务状态，Gateway 决定“是否获授权以及如何安全调用”，Adapter 只处理具体协议。Adapter 不得反向访问业务 Repository。

