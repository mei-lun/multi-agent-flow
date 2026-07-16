# TASK-012 安全 Git CLI 封装
## 前置条件
- TASK-003、TASK-004、TASK-005 已完成。
## 任务内容
- 实现参数数组方式的 Git fetch、show、rev-parse、worktree、commit、push 和分支检查。
## 验收标准
- [ ] 禁止 Shell 字符串拼接和危险 Git 配置注入。
- [ ] 所有路径限制在仓库/工作区根目录。
- [ ] 命令超时、输出限长和凭据脱敏生效。
## 不包含
- 任务状态逻辑。

