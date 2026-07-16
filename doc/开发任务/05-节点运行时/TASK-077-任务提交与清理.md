# TASK-077 任务提交与清理
## 前置条件
- TASK-026、TASK-070、TASK-075、TASK-076、TASK-078 已完成。
## 任务内容
- 打包允许输出、运行自检、commit/push 任务分支、提交 Submission/Blocked/Failed 事件并清理。
## 验收标准
- [ ] push 前再次确认当前 assignment epoch。
- [ ] Submission 包含 base/head、修改路径、测试、问题和剩余项。
- [ ] push 失败可重试且不重复事件；旧 epoch 结果不宣称成功。
## 不包含
- PR 审核和 DONE 状态。

