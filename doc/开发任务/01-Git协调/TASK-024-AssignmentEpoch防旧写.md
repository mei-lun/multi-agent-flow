# TASK-024 Assignment Epoch 防旧写
## 前置条件
- TASK-022、TASK-023 已完成。
## 任务内容
- 实现 assignment_id、递增 epoch、based_on_control_commit 和旧事件 fencing 校验。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-024` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [x] 重分配后旧 epoch 的进度和提交全部拒绝。
- [x] 旧任务分支不会被删除。
- [x] 当前 epoch 的合法事件正常处理。
## 不包含
- 超时判断。
