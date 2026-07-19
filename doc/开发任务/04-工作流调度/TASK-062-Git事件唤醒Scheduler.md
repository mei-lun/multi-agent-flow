# TASK-062 Git 事件唤醒 Scheduler
## 前置条件
- TASK-021、TASK-026、TASK-060、TASK-061 已完成。
## 任务内容
- 将已进入 control 的 Submission/Blocked/Decision 事件按 event_id 唤醒对应 Run。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-062` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [x] 重复事件只唤醒一次。
- [x] 非当前 task/epoch 不能恢复 Graph。
- [x] 失败唤醒可重试并保留记录。
## 不包含
- Gate 决策。
