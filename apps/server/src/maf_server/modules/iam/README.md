# IAM 接口与调用逻辑

## 职责

IAM 只负责本地用户、会话、单组织权限和系统设置。模型 API Key 不属于用户密码，统一交给 Secret Gateway。

## 调用关系

```text
HTTP Router → IamService → PermissionService
                        → IamRepository → maf.db
                        → SecretService（仅敏感系统设置）
                        → Audit/Outbox
```

登录不使用普通 `ActorContext`，其余接口必须先由中间件解析 session。用户禁用与会话撤销必须在同一业务命令中完成，避免已禁用用户继续操作。

## 实现检查

密码永不返回、永不记录日志；登录错误不区分“用户不存在”和“密码错误”；权限查询每次以服务端数据为准；最后一个管理员不能被禁用；更新必须使用乐观锁。

