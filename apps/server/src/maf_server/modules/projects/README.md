# Project 接口与调用逻辑

Project 保存长期配置；Run 启动时复制不可变版本引用，因此后续修改 Project 不得改变已启动 Run。

```text
Project Router → ProjectApplicationService → PermissionService
                                            → ProjectRepository
                                            → Artifact Service（校验输入引用）
                                            → Workflow Service（校验默认版本）
                                            → Inbox Service（变更审批）
                                            → Outbox
```

仓库绑定分两步：本模块只登记；`repositories` 模块调用 Repository Gateway 验证。输入采用追加版本，不提供覆盖接口。运行中变更先形成 ChangeRequest 和站内待办，再由 Scheduler 消费决定，Project Service 不直接改 checkpoint。

