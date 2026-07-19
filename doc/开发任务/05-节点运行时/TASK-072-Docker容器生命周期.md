# TASK-072 Docker 容器生命周期
## 前置条件
- TASK-069～071 已完成。
## 任务内容
- 实现 create/start/logs/stop/remove，使用非特权、只读根、cap-drop 和资源上限。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-072` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [x] 禁止 Docker Socket、host network、设备和任意宿主挂载。
- [x] 超时/异常/epoch 变化都进入同一清理流程。
- [x] 只删除带本节点 task label 的资源。
## 不包含
- Agent 逻辑。
