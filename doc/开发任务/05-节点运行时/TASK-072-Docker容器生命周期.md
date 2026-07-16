# TASK-072 Docker 容器生命周期
## 前置条件
- TASK-069～071 已完成。
## 任务内容
- 实现 create/start/logs/stop/remove，使用非特权、只读根、cap-drop 和资源上限。
## 验收标准
- [ ] 禁止 Docker Socket、host network、设备和任意宿主挂载。
- [ ] 超时/异常/epoch 变化都进入同一清理流程。
- [ ] 只删除带本节点 task label 的资源。
## 不包含
- Agent 逻辑。

