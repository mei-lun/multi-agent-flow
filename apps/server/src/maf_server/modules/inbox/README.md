# Inbox 接口与人工信号

```text
Gate/Tool/Change Request → InboxService.create → OPEN item
用户决定 → expected_subject_version 校验 → Decision + CLOSED
                                          → approval.decided Outbox
                                          → Scheduler Wakeup
```

只支持站内待办。浏览器关闭不影响待办存在；Scheduler 不能依赖 WebSocket 在线状态。人工决策是事实记录，提交后不可修改，只能通过新的纠正流程处理。

