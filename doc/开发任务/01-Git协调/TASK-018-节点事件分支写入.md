# TASK-018 节点事件分支写入
## 前置条件
- TASK-011～014 已完成。
## 任务内容
- 在 `maf/node/<node-id>` 追加唯一事件文件、提交并 fast-forward push。
## 验收标准
- [ ] push 冲突可 fetch/rebase 后用同 event_id 重试。
- [ ] 节点只能写自己的事件分支。
- [ ] 事件文件通过 event-v1 Schema。
## 不包含
- 中央接受事件。

