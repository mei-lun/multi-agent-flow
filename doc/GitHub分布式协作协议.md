# GitHub 分布式协作协议

> 协议版本：1  
> 状态：已确认  
> 适用范围：不同机器之间的功能级任务协作

## 1. 已确认决策

1. 禁止自建节点—控制面 HTTP 协议；节点之间不直接通信；
2. 允许使用 Git pull/push，并允许中央调度器使用 GitHub 官方能力管理 Pull Request；
3. GitHub 协调分支是跨机器唯一事实源，中央 SQLite 是可重建投影；
4. 只有中央调度器可写权威 `maf/control` 分支；
5. 节点只写自己的事件分支和被分配任务的工作分支；
6. GitHub 只协调功能、模块、文档、测试等粗粒度任务；Agent 内部步骤留在节点本地。

## 2. 分支协议

| 分支 | 写入者 | 用途 |
|---|---|---|
| `main` | 最终评审流程 | 已验收代码 |
| `maf/control` | 中央调度器 | 权威任务、节点、已接受事件和状态总览 |
| `maf/node/<node-id>` | 对应节点 | 追加 Claim/Progress/Blocked/Submission 等事件 |
| `maf/task/<task-id>/e<epoch>-<node-id>` | 当前被分配节点 | 代码、文档、测试和交付证据 |

`maf/control` 必须启用保护策略。节点提交到 control 的直接 push 一律拒绝。

## 3. 权威目录

```text
.maf/
├── PROTOCOL.md
├── project.yaml
├── schemas/
│   ├── task-v1.schema.json
│   ├── node-v1.schema.json
│   └── event-v1.schema.json
├── tasks/<task-id>.yaml
├── nodes/<node-id>.yaml
├── events/<yyyy-mm>/<event-id>.json
└── status.md
```

- `tasks/`、`nodes/`、`events/` 是机器可读事实；
- `status.md` 是中央调度器生成的人类视图，禁止手工修改；
- 每个任务独立文件，避免无关任务争用同一文档；
- 所有文件先通过 Schema 和状态转换校验才能进入 control。

## 4. 节点身份

节点首次初始化生成随机持久 UUID，格式 `node-<uuid>`，不使用 CPU、网卡或硬盘序列号。节点清单记录显示名、Git 身份/签名指纹、能力标签、支持模型别名、Docker Profile、并发容量和软件版本。

节点提交者身份必须与已登记指纹一致。节点不能在事件中临时声明未登记能力。

## 5. 任务状态机

```text
PLANNED → READY → ASSIGNED → IN_PROGRESS → SUBMITTED → REVIEWING → DONE
                       ├────→ BLOCKED
                       ├────→ REWORK_REQUIRED
                       ├────→ LEASE_EXPIRED → READY/ASSIGNED
                       ├────→ FAILED
                       └────→ CANCELLED
```

节点只能申请或报告状态，不能直接决定权威状态。中央调度器拥有全部状态转换写权；`DONE` 只能在评审、测试、验收和合并完成后写入。

## 6. 任务认领

1. 节点 fetch `maf/control` 并读取最新 task/node/schema；
2. 节点选择状态 READY、依赖完成且能力匹配的任务；
3. 节点向 `maf/node/<node-id>` 追加唯一 `CLAIM_REQUESTED` 事件；
4. 中央调度器 fetch 所有 node 分支，按 event_id 幂等处理；
5. 调度器再次检查任务状态、依赖、能力、容量和优先级；
6. 只接受一个申请，生成 `assignment_id` 和递增 `assignment_epoch`；
7. 调度器更新 `maf/control` 中任务为 ASSIGNED；
8. 节点再次 fetch，确认 control 中 owner/epoch 与自己一致后才能开始；
9. 未被接受的申请记录为 REJECTED，不产生工作权限。

## 7. 防止旧节点覆盖

每条进度、阻塞和提交事件必须携带 `task_id`、`node_id`、`assignment_id`、`assignment_epoch` 和 `based_on_control_commit`。

任务重新分配时 epoch 递增。旧 epoch 的事件不会更新权威状态；旧分支保留为可选恢复材料。这个 epoch 是分布式 fencing token，不能用时间戳替代。

## 8. 进度和超时

节点在以下情况写进度事件：

- 完成一个可验证里程碑；
- completed/remaining checklist 变化；
- 进度至少变化 5%；
- 发现或解决阻塞问题；
- 长任务 15 分钟没有其他事件。

进度必须包含已完成项、剩余项、问题、是否阻塞、当前代码 head、测试摘要和预计下一步。中央调度器以收到有效事件的时间更新 SQLite 投影和任务 `last_progress_at`。

首版建议 assignment 超时 60 分钟、警告 30 分钟、宽限 15 分钟。超时后调度器先检查远程任务分支是否有新提交，再决定 `LEASE_EXPIRED` 和重新分配。

## 9. 交付与验收

节点完成任务后：

1. 在任务分支提交代码、文档和测试；
2. push 工作分支；
3. 追加 `SUBMISSION_CREATED` 事件，包含 head/base、修改清单、测试、已知问题和剩余内容；
4. 调度器验证 epoch、提交可达性、base、Schema 和证据；
5. 调度器更新 SUBMITTED/REVIEWING，并创建或更新 PR；
6. 评审、测试和产品验收通过后进行最终人工合并；
7. merge commit 固定后，调度器写 DONE。

节点声明“完成”只表示提交候选结果，不等于系统接受。

## 10. Git 与 SQLite 一致性

- Git control commit 是投影水位；SQLite 保存 `projector_control_commit`；
- 每个事件以 event_id 去重；
- 调度器先生成并推送 control commit，确认远端可见后再推进 SQLite 投影水位；
- SQLite 更新失败时从上一个水位重放 Git diff/events；
- 禁止节点连接中央 SQLite；
- 节点本地 SQLite 只保存本机 Agent Attempt、缓存、日志索引和未推送事件，可随时重建。

## 11. 故障处理

| 故障 | 处理 |
|---|---|
| 两节点同时申请 | 中央只接受一个，其他事件拒绝 |
| 节点失联 | 超时、epoch 递增、重新分配 |
| 旧节点恢复 | 旧 epoch 结果隔离，可人工挑选提交 |
| 节点 push 失败 | fetch/rebase 自己的事件分支后按同 event_id 重试 |
| control push 冲突 | 中央调度器重新 fetch；单写者下仍冲突则停止并报警 |
| 调度器宕机 | 节点可继续当前工作，但不能获得新分配；恢复后重放事件 |
| SQLite 损坏 | 从 control 和 canonical events 重建 |
| PR head 在审批后变化 | expected head 不匹配，重新评审 |

## 12. 不使用 Git 协调的内容

以下高频或敏感内容只留在节点本地：模型流式增量、每步思考、Tool 调用中间状态、Docker 心跳、密钥、完整环境变量和高频日志。任务分支只提交允许交付的源代码、文档、测试和脱敏报告；大文件必须先配置 Git LFS，否则拒绝提交。

