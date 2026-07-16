"""IAM 应用用例接口；这里只描述逻辑顺序，不实现密码或会话机制。"""

from typing import Protocol

from maf_contracts.common import ActorContext

from .schemas import (
    CreateUserRequest,
    LoginRequest,
    PutSettingRequest,
    SessionView,
    SettingView,
    UpdateUserRequest,
    UserPage,
    UserQuery,
    UserView,
)


class IamService(Protocol):
    async def login(self, request: LoginRequest) -> SessionView:
        """验证本地用户名与密码并创建会话。

        输入密码只在内存中用于哈希比较，不能写日志或审计 payload。实现顺序：规范化
        用户名、读取用户、检查 ACTIVE、恒定时间验证密码、创建有过期时间的会话、记录
        登录审计。成功返回会话及脱敏用户；失败统一返回认证失败，避免泄露用户名是否存在。
        """
        ...

    async def logout(self, actor: ActorContext, session_id: str) -> None:
        """撤销当前会话。

        只能撤销调用者自己的 session。重复注销视为成功；提交后该 session 的后续请求必须
        返回未认证。产生 ``auth.session.revoked`` 审计记录，不删除历史。
        """
        ...

    async def get_current_user(self, actor: ActorContext) -> UserView:
        """返回当前用户与实时有效权限，不信任浏览器缓存的权限列表。"""
        ...

    async def list_users(self, actor: ActorContext, query: UserQuery) -> UserPage:
        """分页查询单组织用户；先校验管理权限，再应用状态和关键字过滤。"""
        ...

    async def create_user(self, actor: ActorContext, request: CreateUserRequest) -> UserView:
        """创建本地用户。

        校验管理员权限、用户名唯一性、密码规则及权限键有效性；密码必须先做强哈希再保存。
        同一幂等键不得创建两个用户。输出不包含密码哈希，产生 ``iam.user.created`` 事件。
        """
        ...

    async def update_user(
        self, actor: ActorContext, user_id: str, request: UpdateUserRequest
    ) -> UserView:
        """按 expected_version 更新用户资料、状态或权限。

        禁止操作者禁用系统最后一个管理员。版本不一致返回冲突；禁用后撤销目标用户全部
        会话。输出为更新后的用户视图并产生审计事件。
        """
        ...

    async def get_setting(self, actor: ActorContext, key: str) -> SettingView:
        """读取允许公开给当前管理员的系统设置；Secret 只返回是否已配置。"""
        ...

    async def put_setting(
        self, actor: ActorContext, key: str, request: PutSettingRequest
    ) -> SettingView:
        """创建或更新一个受支持的系统设置。

        按 key 查找预定义 Schema，验证值、权限、expected_version 和幂等键；敏感配置必须
        转存 SecretStore，业务表仅保存引用。返回新版本，不返回密钥明文。
        """
        ...


class PermissionService(Protocol):
    async def require(self, actor: ActorContext, action: str, resource: str) -> None:
        """无返回表示允许；无匹配授权、上下文缺失或策略异常都必须拒绝。"""
        ...

    async def list_effective_permissions(self, actor: ActorContext) -> list[str]:
        """计算当前主体的实时有效权限键，供 `/me` 和界面显示使用。"""
        ...

