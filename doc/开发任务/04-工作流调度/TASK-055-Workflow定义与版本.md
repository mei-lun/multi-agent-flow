# TASK-055 Workflow 定义与版本
## 前置条件
- TASK-031、TASK-053 已完成。
## 任务内容
- 实现 Workflow Definition、DRAFT Version、基于旧版本复制和变更摘要。
## 允许改动文件
- 仅限《任务范围清单》中 `TASK-055` 行列出的生产文件和测试文件。
## 允许新增或修改接口
- 仅限该行列出的接口/类型；其他公共接口只允许调用。
## 禁止改动
- 未列出的文件、接口、数据库表、API 路径和 Git Schema。
## 验收标准
- [ ] Definition key 唯一。
- [ ] 复制后新旧版本独立。
- [ ] PUBLISHED 版本不能修改。
## 不包含
- Graph 编辑。
