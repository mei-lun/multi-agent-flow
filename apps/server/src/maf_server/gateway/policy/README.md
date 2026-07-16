# Policy 接口逻辑

权限取交集：系统策略 ∩ Role Version ∩ Git Task Grant ∩ 当前 assignment epoch ∩ 本次参数约束。任何一层缺失均拒绝。Casbin 判断资源动作关系，Python validators 检查具体路径、域名、金额、步数、Tool 参数和网络范围。
