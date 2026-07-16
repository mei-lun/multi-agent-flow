# TASK-059 Run 创建与不可变快照
## 前置条件
- TASK-033～035、TASK-043、TASK-058、TASK-078 已完成。
## 任务内容
- 校验项目/输入/仓库/发布版本/预算，生成 Run Snapshot 和 CREATED Run。
## 验收标准
- [ ] 相同幂等键只创建一个 Run。
- [ ] 快照固定 Role/Skill/Tool/Model/Policy/control base commit。
- [ ] 创建失败不留下半成品 Run。
## 不包含
- LangGraph 推进。

