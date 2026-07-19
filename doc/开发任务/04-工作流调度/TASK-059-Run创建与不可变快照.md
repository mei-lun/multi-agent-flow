# TASK-059 Run 创建与不可变快照
## 前置条件
- TASK-033～035、TASK-043、TASK-058、TASK-078 已完成。
## 任务内容
- 校验项目/输入/仓库/发布版本/预算，生成 Run Snapshot 和 CREATED Run。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-059` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [x] 相同幂等键只创建一个 Run。
- [x] 快照固定 Role/Skill/Tool/Model/Policy/control base commit。
- [x] 创建失败不留下半成品 Run。
## 不包含
- LangGraph 推进。
