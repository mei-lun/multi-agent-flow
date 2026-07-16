# Workflow 接口与调用逻辑

```text
Definition → DRAFT Version → 保存 Graph → 静态检查 → PUBLISHED
                                              ↓
Run 创建 → 固定 PUBLISHED Version → graph_builder → LangGraph checkpoint
```

Graph 编辑采用完整替换，避免前端局部操作造成丢边；并发用 expected_version 检测。条件表达式不执行 Python/JavaScript，只允许受限语法。发布检查必须重新运行，不能复用可能过期的旧 PASS 报告。

