# TASK-053 Role 发布校验
## 前置条件
- TASK-052 已完成。
## 任务内容
- 校验模型能力、Skill 发布状态、Skill Tool 依赖、Tool Grant、Policy、网络和资源上限。
## 验收标准
- [ ] 一次返回全部错误和警告。
- [ ] Skill 声明但 Role 未绑定的 Tool 阻断发布。
- [ ] 发布时重新校验并计算 content hash。
## 不包含
- Role 试运行。

