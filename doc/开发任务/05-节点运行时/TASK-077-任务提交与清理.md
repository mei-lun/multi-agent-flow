# TASK-077 任务提交与清理
## 前置条件
- TASK-026、TASK-070、TASK-075、TASK-076、TASK-078 已完成。
## 任务内容
- 打包允许输出、运行自检、commit/push 任务分支、提交 Submission/Blocked/Failed 事件并清理。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-077` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [ ] push 前再次确认当前 assignment epoch。
- [ ] Submission 包含 base/head、修改路径、测试、问题和剩余项。
- [ ] push 失败可重试且不重复事件；旧 epoch 结果不宣称成功。
## 不包含
- PR 审核和 DONE 状态。
