# Repository Gateway 接口与调用逻辑

```text
verify binding → resolve immutable base commit
Run start → export base bundle Artifact → Runner worktree
Runner result → Patch + producer commit/tree → Server validate/reapply
              → integration branch → GitHub PR / Local Review
              → refresh checks/head
Final gate → expected head compare → merge
```

Git CLI 只接收参数数组。Runner 不接触仓库 Key。所有 push、PR 和 merge 都在 Server Gateway 中执行，并使用 idempotency key。最终业务门禁由 RepositoryApplicationService/Scheduler 判断，Adapter 只执行已经授权的仓库命令。

