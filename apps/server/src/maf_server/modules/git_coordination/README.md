# Git Coordination 接口与调用逻辑

## 职责

该模块是跨机器协调的唯一入口。它 fetch/push GitHub 仓库、验证节点事件、单写 `maf/control`、生成 `status.md`，并把已确认 Git 状态投影到中央 SQLite。它不通过 HTTP 向节点派发任务。

```text
Scheduler → publish_tasks → maf/control
Node branch maf/node/<id> → fetch → event validation → state transition
                                      → control commit → SQLite projection
Node task branch → SUBMISSION_CREATED → review/PR → merge → DONE control commit
```

Git 先于 SQLite：control push 成功后投影数据库；投影失败从 commit 水位重放。节点事件只是申请，只有进入 control 后才成为事实。

