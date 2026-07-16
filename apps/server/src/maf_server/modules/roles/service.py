"""Role 配置、校验、试运行和发布接口。"""

from typing import Protocol
from maf_contracts.common import ActorContext
from .schemas import *


class RoleVersionValidator(Protocol):
    async def validate(self, draft: CreateRoleVersionRequest) -> ValidationReport:
        """验证角色版本的全部引用和权限闭包。

        检查 Model Policy 可用；所有 Skill 已发布；Skill 声明的 Tool 都出现在 tool_grants；
        Tool Grant 不超 Capability Policy；网络访问不超 Network Policy；资源与预算为正且在
        系统上限内。收集所有错误后一次返回，不在此方法发布版本。
        """
        ...


class RoleService(Protocol):
    async def create_role(self, actor: ActorContext, request: CreateRoleRequest) -> RoleView:
        """创建稳定 Role Definition；同组织 key 唯一，成功后尚不可用于运行。"""
        ...

    async def create_version(
        self, actor: ActorContext, role_id: str, request: CreateRoleVersionRequest
    ) -> RoleVersionView:
        """创建 DRAFT Role Version 并固定所有引用的精确版本。

        先做格式校验和 RoleVersionValidator；允许带警告保存草稿，但存在 error 不得发布。
        Prompt 正文进入受控存储，响应仅返回哈希和配置摘要。
        """
        ...

    async def dry_run(
        self, actor: ActorContext, version_id: str, request: DryRunRoleRequest
    ) -> DryRunRoleResult:
        """创建隔离的单角色测试 Run。

        使用该 DRAFT 的精确权限，不能因是管理员发起就放宽 Tool/Skill；成本不得超过请求和
        系统上限较小值。返回测试运行引用，完整输出作为 Artifact。
        """
        ...

    async def publish(
        self, actor: ActorContext, version_id: str, request: PublishRoleRequest
    ) -> RoleVersionView:
        """乐观锁发布 Role Version。

        重新运行 Validator，确认所有依赖仍已发布且未禁用，再把状态改为 PUBLISHED 并生成
        content_hash。发布后禁止修改；后续变化创建新版本。
        """
        ...

