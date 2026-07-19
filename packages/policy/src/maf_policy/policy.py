"""Casbin RBAC policy loading and PermissionService implementation.

TASK-031 范围：
- 定义 5 个内置角色（ADMIN/DESIGNER/OWNER/APPROVER/OBSERVER）及其默认 Casbin policy。
- 实现 ``CasbinPermissionService``，提供 ``require`` 与 ``list_effective_permissions``。
- 无策略匹配或策略异常默认拒绝（raise ``PermissionDeniedError``）。

设计决策：
- Subject = 角色名（permission_key）。``ActorContext.permission_keys`` 携带调用者
  被授予的角色列表；``require`` 逐角色调用 ``enforcer.enforce``，任一通过即放行。
- Casbin model 使用 ``keyMatch2`` 匹配资源（支持 ``*`` 和 ``:param``），``regexMatch``
  匹配动作（支持 ``.*``、``(read|write)`` 等）。
- 默认 policy 覆盖系统级资源（``users``、``projects``、``skills``、``tools``、
  ``workflows``、``model_connections``、``reviews``、``inbox``、``settings``）。

对应《多 Agent 协同工具系统设计文档》：
- 7.1 ``user_permissions`` 表 ``permission_set`` 取值 ADMIN/DESIGNER/OWNER/APPROVER/OBSERVER；
- 11.1 ``PermissionService`` Protocol；
- 11.5 Casbin 负责"主体能否对资源执行动作"。
"""

from __future__ import annotations

from pathlib import Path

import casbin
from casbin import Enforcer

from maf_contracts.common import ActorContext
from maf_domain.errors import PermissionDeniedError

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: Casbin model 文件路径（``packages/policy/model.conf``）。
MODEL_CONF_PATH: Path = Path(__file__).resolve().parents[2] / "model.conf"

#: 5 个内置角色，与设计文档 7.1 ``permission_set`` 取值一致。
#: - ADMIN（管理员）：系统全权管理，包括用户、设置、模型连接等。
#: - DESIGNER（设计者）：配置 Skill/Tool/Workflow/模型连接等能力资源。
#: - OWNER（项目负责人）：管理项目及其输入、仓库绑定和变更请求。
#: - APPROVER（审批人）：处理 Review、Inbox 决策和合并审批。
#: - OBSERVER（观察者）：只读查看系统资源。
KNOWN_ROLES: frozenset[str] = frozenset(
    {"ADMIN", "DESIGNER", "OWNER", "APPROVER", "OBSERVER"}
)

#: 默认 Casbin policy 行：(sub=角色, obj=资源模式, act=动作正则)。
#:
#: 资源命名使用简单标识符（如 ``users``、``projects``），由调用方在
#: ``PermissionService.require(actor, action, resource)`` 传入。
#: ``keyMatch2`` 支持 ``*`` 通配整个资源段，``regexMatch`` 支持 ``.*`` 通配动作。
DEFAULT_POLICIES: list[tuple[str, str, str]] = [
    # ADMIN：系统全权
    ("ADMIN", "*", ".*"),
    # DESIGNER：能力资源读写，用户只读
    ("DESIGNER", "skills", "(read|write)"),
    ("DESIGNER", "tools", "(read|write)"),
    ("DESIGNER", "workflows", "(read|write)"),
    ("DESIGNER", "model_connections", "(read|write)"),
    ("DESIGNER", "users", "read"),
    ("DESIGNER", "settings", "read"),
    # OWNER：项目管理，用户只读
    ("OWNER", "projects", ".*"),
    ("OWNER", "repositories", "(read|write)"),
    ("OWNER", "users", "read"),
    # APPROVER：审批流，用户只读
    ("APPROVER", "reviews", ".*"),
    ("APPROVER", "inbox", ".*"),
    ("APPROVER", "users", "read"),
    # OBSERVER：全局只读
    ("OBSERVER", "*", "read"),
]


# --------------------------------------------------------------------------- #
# PermissionService 实现
# --------------------------------------------------------------------------- #


class CasbinPermissionService:
    """基于 Casbin 的 RBAC 权限服务实现。

    依赖注入：
        - ``model_path``：Casbin model 文件路径，默认 ``packages/policy/model.conf``。
        - ``policies``：Casbin policy 行列表，默认使用 ``DEFAULT_POLICIES``。

    设计规则（对应设计文档 11.1）：
        - 权限缓存最多 60 秒（由认证中间件负责刷新 ``ActorContext``，
          本服务每次调用都基于传入的 ``actor`` 即时判定，不做本地缓存）；
        - 无匹配授权、上下文缺失或策略异常都拒绝；
        - Agent 身份不经过本服务获取管理员权限。

    安全约束：
        - ``require`` 在任何异常路径下都 ``raise PermissionDeniedError``，
          绝不在策略异常时放行；
        - 不记录敏感参数到日志（action/resource 是业务字段，非敏感）。
    """

    def __init__(
        self,
        model_path: str | Path | None = None,
        policies: list[tuple[str, str, str]] | None = None,
    ) -> None:
        self._model_path: str = str(model_path or MODEL_CONF_PATH)
        self._policies: list[tuple[str, str, str]] = list(
            policies if policies is not None else DEFAULT_POLICIES
        )
        self._enforcer: Enforcer = self._build_enforcer()

    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #

    def _build_enforcer(self) -> Enforcer:
        """构造 Casbin Enforcer，加载 model 和默认 policy。

        若 model 文件缺失或 policy 加载异常，抛 ``RuntimeError``；
        调用方（通常是 service 层）应在启动时捕获并 fail-fast。
        """
        if not Path(self._model_path).exists():
            raise RuntimeError(
                f"Casbin model 文件不存在: {self._model_path}"
            )
        enforcer = casbin.Enforcer(self._model_path)
        for sub, obj, act in self._policies:
            enforcer.add_policy(sub, obj, act)
        return enforcer

    # ------------------------------------------------------------------ #
    # PermissionService Protocol 实现
    # ------------------------------------------------------------------ #

    async def require(
        self, actor: ActorContext, action: str, resource: str
    ) -> None:
        """检查 actor 是否可以对 resource 执行 action；无返回表示允许。

        判定顺序：
        1. 校验 ``actor`` 必须是 dict 且含非空 ``user_id``；
        2. 校验 ``actor.permission_keys`` 必须是非空 list；
        3. 逐角色调用 ``enforcer.enforce(role, resource, action)``；
        4. 任一角色通过即放行（return None）；
        5. 全部不通过或策略异常 → ``raise PermissionDeniedError``。

        :param actor: 当前调用者上下文（由认证中间件构造）。
        :param action: 动作标识，如 ``"read"``、``"write"``。
        :param resource: 资源标识，如 ``"users"``、``"projects"``。
        :raises PermissionDeniedError: 无匹配授权、上下文缺失或策略异常。
        """
        if not isinstance(actor, dict):
            raise PermissionDeniedError("权限不足：调用者上下文缺失")

        user_id = actor.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise PermissionDeniedError("权限不足：未认证")

        permission_keys = actor.get("permission_keys")
        if not isinstance(permission_keys, list) or not permission_keys:
            raise PermissionDeniedError("权限不足：无授权角色")

        try:
            for role in permission_keys:
                if not isinstance(role, str) or not role:
                    continue
                if self._enforcer.enforce(role, resource, action):
                    return  # 允许
        except PermissionDeniedError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 策略异常默认拒绝
            raise PermissionDeniedError(
                "权限检查异常",
                context={
                    "action": action,
                    "resource": resource,
                    "cause": str(exc),
                },
            ) from exc

        raise PermissionDeniedError(
            "权限不足",
            context={"action": action, "resource": resource},
        )

    async def list_effective_permissions(
        self, actor: ActorContext
    ) -> list[str]:
        """返回当前主体的实时有效权限键（角色列表）。

        ``ActorContext`` 由认证中间件每次请求从 DB 重新构造，
        ``permission_keys`` 即为实时有效角色。本方法直接返回，
        不做本地缓存，保证权限撤销通过版本号立即失效。
        """
        if not isinstance(actor, dict):
            return []
        keys = actor.get("permission_keys")
        if not isinstance(keys, list):
            return []
        return [str(k) for k in keys if isinstance(k, str) and k]

    # ------------------------------------------------------------------ #
    # 管理接口（供测试和运维使用）
    # ------------------------------------------------------------------ #

    def check_permission(
        self, role: str, resource: str, action: str
    ) -> bool:
        """低层权限检查：单个角色是否允许对资源执行动作。

        供测试和策略调试使用。生产代码应通过 ``require`` 检查 actor。
        策略异常返回 False（默认拒绝）。
        """
        try:
            return bool(self._enforcer.enforce(role, resource, action))
        except Exception:  # noqa: BLE001 —— 策略异常默认拒绝
            return False

    def add_policy(self, role: str, resource: str, action: str) -> bool:
        """添加一条 Casbin policy；已存在返回 False。"""
        return bool(self._enforcer.add_policy(role, resource, action))

    def remove_policy(self, role: str, resource: str, action: str) -> bool:
        """移除一条 Casbin policy；不存在返回 False。"""
        return bool(self._enforcer.remove_policy(role, resource, action))

    def list_policies(self) -> list[tuple[str, str, str]]:
        """返回当前所有 policy 行（调试用）。"""
        return [tuple(p) for p in self._enforcer.get_policy()]

    def reload(self) -> None:
        """重新加载 enforcer（应用 ``self._policies`` 的变更）。"""
        self._enforcer = self._build_enforcer()


# --------------------------------------------------------------------------- #
# 角色校验工具（供 IAM service 在 create_user/update_user 时校验 permission_keys）
# --------------------------------------------------------------------------- #


def validate_permission_keys(permission_keys: list[str]) -> list[str]:
    """校验 permission_keys 列表中每个 key 都是已知角色。

    :param permission_keys: 待校验的角色列表。
    :returns: 去重后的合法角色列表（保持输入顺序）。
    :raises ValueError: 若包含未知角色。
    """
    if not isinstance(permission_keys, list):
        raise ValueError("permission_keys 必须是列表")
    seen: set[str] = set()
    result: list[str] = []
    for key in permission_keys:
        if not isinstance(key, str) or not key:
            raise ValueError("permission_key 不能为空")
        if key not in KNOWN_ROLES:
            raise ValueError(
                f"未知角色: {key}，合法角色: {sorted(KNOWN_ROLES)}"
            )
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


__all__ = [
    "DEFAULT_POLICIES",
    "KNOWN_ROLES",
    "MODEL_CONF_PATH",
    "CasbinPermissionService",
    "validate_permission_keys",
]
