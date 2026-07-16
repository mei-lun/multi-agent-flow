# Repository 接口与合并门禁

```text
Project 绑定 → verify → 固定 base branch/commit
代码 Attempt → Patch/Git bundle Artifact → Repository Gateway 受控应用
             → integration branch → PR → checks/review projection
最终人工 APPROVE + 所有 Gate PASS + head 未变化 → merge
```

Runner 不获得仓库长期凭据，也不直接 push。Server Gateway 在受控工作区重新应用变更。最终合并必须比较 expected head，防止用户审批后 PR 又发生变化。本地 Git 没有远端 PR 时也创建等价 Review 记录和显式合并门禁。

