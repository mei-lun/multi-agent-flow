"""本地 bare/working-tree Git Adapter 接口说明。

实现必须把配置路径解析到允许的 repository roots 内，使用独立 worktree，不能在用户当前
工作树直接清理、reset 或覆盖未提交文件。本地 Review 使用系统内记录代替 GitHub PR。
"""
