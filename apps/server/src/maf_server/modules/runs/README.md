# Run 接口与状态逻辑

Run 是已发布配置的不可变执行快照，不是 Project 当前配置的实时引用。

```text
Start API → RunService → Snapshot Artifact + Run CREATED + Outbox
                         → Scheduler.start_run
                         → maf/control task → Node task branch → Submission Event
                         → Scheduler wakeup → Gate/Route → 下一 Task

Pause/Resume/Cancel/Budget/Retry API → 幂等命令记录 → Scheduler Signal
```

所有控制命令先持久化再通知 Scheduler。Task 是某 Workflow Node 的逻辑工作，Attempt 是一次具体执行；重试只能新增 Attempt，不能清空旧失败记录。HTTP 查询读投影表，Scheduler checkpoint 不是查询 API。
