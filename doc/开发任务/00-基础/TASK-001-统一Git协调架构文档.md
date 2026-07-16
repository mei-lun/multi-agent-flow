# TASK-001 统一 Git 协调架构文档

## 前置条件
- 已确认禁止自建节点 HTTP、Git 为跨机器事实源、control 单写和功能级任务粒度。
## 任务内容
- 清理 PRD、需求分析、系统设计、框架和接口文档中的 Runner HTTP、中央队列、Attempt Token 旧设计。
- 将 Git 分支、文件、事件、assignment epoch 和 SQLite 投影写成唯一基线。
## 验收标准
- [ ] 文档中不存在可执行的跨节点 `/internal/v1` 协议。
- [ ] 五类核心文档对事实源、写入权和节点工作流描述一致。
- [ ] `doc/GitHub分布式协作协议.md` 被所有相关文档引用。
## 不包含
- Git 协调代码实现。

