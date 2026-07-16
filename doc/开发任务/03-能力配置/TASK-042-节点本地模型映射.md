# TASK-042 节点本地模型映射
## 前置条件
- TASK-013、TASK-029、TASK-041 已完成。
## 任务内容
- 将 control 中逻辑模型别名映射到节点本地连接/Profile/Secret，并在节点清单声明可用别名。
## 验收标准
- [ ] Git 文件不含 Key 或本地 Secret ID。
- [ ] 节点缺少所需别名时不能 Claim。
- [ ] 映射变化不会改变已开始 assignment 的快照。
## 不包含
- 模型调用循环。

