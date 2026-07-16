# Review 与 Gate 调用逻辑

Agent 可以生成 Review Artifact，但是否通过由确定性 QualityGateService 根据 Rubric、Schema 和阻断项计算。代码评审与测试可并行，产品验收等待两者完成；最终 PR 合并还要等待人工 Gate。

```text
Attempt outputs → Artifact Validators → Review records
                                  └────→ QualityGateService
                                           ├─ PASS → Scheduler route
                                           ├─ REWORK → 指定责任节点
                                           ├─ WAITING_HUMAN → Inbox
                                           └─ FAIL → Run failure path
```

