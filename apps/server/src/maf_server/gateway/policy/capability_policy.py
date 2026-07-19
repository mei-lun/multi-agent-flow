"""CapabilityPolicyService concrete implementation.

TASK-050 范围：
- 在 ``gateway/policy/`` 提供 ``CapabilityPolicyServiceImpl`` 具体类，
  实现 ``evaluate`` / ``evaluate_batch`` / ``set_policy`` / ``get_policy`` /
  ``list_policies`` 接口。
- 控制节点/角色可以使用哪些能力（Tool、Model、Skill）。
- **默认拒绝**：未匹配任何策略时返回 ``allowed=False``。
- **ADMIN 全允许**：``actor_roles`` 含 ``ADMIN`` 时短路放行。
- ``set_policy`` 仅 ADMIN 可调用（``actor_id`` 必须传入且 actor_roles 由
  调用方在更高层校验为 ADMIN；本服务在 set_policy 内再次断言）。

与 ``gateway/policy/service.py`` 中既有 Protocol 的关系：
- 既有 ``CapabilityPolicyService`` Protocol 的 ``evaluate`` 签名是
  ``(subject, action, resource, context)``，对应 Tool Gateway 的旧契约。
- 本文件按 TASK-050 任务说明新增 ``evaluate(request: CapabilityRequest)``
  签名。两个签名并存：旧 Protocol 保留为接口契约，本类提供新签名实现。
  未来 Tool Gateway（TASK-051）可逐步迁移到新签名。

与 ``CasbinPermissionService``（TASK-031）的关系：
- Casbin 管粗粒度资源访问（read/write/manage 资源类）；
- CapabilityPolicy 管细粒度能力使用（哪个 Tool/Model/Skill 可用）；
- 两者正交，本服务不调用 Casbin。

安全约束：
- 任何异常路径返回 ``allowed=False``（fail-closed），不抛出未捕获异常；
- ``set_policy`` 校验 ``actor_id`` 与 ADMIN 角色，否则抛
  ``PermissionDeniedError``，避免误写策略；
- 策略评估时存储读取异常返回 ``DEFAULT_DENY``。
"""

from __future__ import annotations

from typing import Any, Mapping

from maf_domain.errors import ArgumentError, PermissionDeniedError
from maf_policy.capability import (
    REASON_ADMIN_DEFAULT_ALLOW,
    REASON_DEFAULT_DENY,
    REASON_INVALID_REQUEST,
    REASON_POLICY_ALLOWED,
    REASON_POLICY_DENIED,
    REASON_POLICY_ERROR,
    ALL_CAPABILITY_TYPES,
    CapabilityDecision,
    CapabilityPolicy,
    CapabilityPolicyStore,
    CapabilityRequest,
    InMemoryCapabilityPolicyStore,
    PolicyRule,
    allow_decision,
    deny_decision,
    rule_matches,
    seed_default_policies,
)

#: ADMIN 角色常量（与 ``maf_policy.KNOWN_ROLES`` 一致）。
ADMIN_ROLE: str = "ADMIN"


class CapabilityPolicyServiceImpl:
    """能力策略评估服务具体实现。

    依赖注入：
        - ``store``：``CapabilityPolicyStore``，默认 ``InMemoryCapabilityPolicyStore``，
          启动时植入默认策略（ADMIN 全允许 + OBSERVER tool 列表只读示例）。
        - ``seed_defaults``：构造时是否植入默认策略，默认 ``True``。

    评估顺序（对应 ``evaluate``）：
        1. 校验 ``request`` 基本字段（actor_id / actor_roles / capability_type /
           capability_name），缺失或非法返回 ``INVALID_REQUEST`` 拒绝；
        2. 若 ``actor_roles`` 含 ``ADMIN``：短路返回 ``ADMIN_DEFAULT_ALLOW``
           （即使 store 为空也放行，确保管理员不会被锁死）；
        3. 加载 ``store.list_policies()``，过滤 ``enabled=True``，按
           ``priority`` 降序排序；
        4. 逐策略、逐规则匹配，**首条命中即返回**该规则的 decision；
        5. 全部未命中 → 返回 ``DEFAULT_DENY``。

    ``set_policy`` 仅 ADMIN 可调用：调用方传入 ``actor_id``，本服务假定
    调用方已通过 Casbin ``require(actor, "write", "capability_policies")``
    完成资源级写权限检查；本服务额外断言 ``actor_id`` 非空。
    """

    def __init__(
        self,
        store: CapabilityPolicyStore | None = None,
        *,
        seed_defaults: bool = True,
    ) -> None:
        self._store: CapabilityPolicyStore = (
            store if store is not None else InMemoryCapabilityPolicyStore()
        )
        if seed_defaults:
            seed_default_policies(self._store)

    # ------------------------------------------------------------------ #
    # 属性
    # ------------------------------------------------------------------ #

    @property
    def store(self) -> CapabilityPolicyStore:
        """暴露底层存储，供测试与运维查询。"""
        return self._store

    # ------------------------------------------------------------------ #
    # evaluate / evaluate_batch
    # ------------------------------------------------------------------ #

    async def evaluate(self, request: CapabilityRequest) -> CapabilityDecision:
        """评估单个能力使用请求。

        :param request: ``CapabilityRequest``，必须含非空 ``actor_id``、
            ``actor_roles``、``capability_type``、``capability_name``。
        :returns: ``CapabilityDecision``，含 ``allowed`` / ``reason`` /
            ``conditions``；任何异常路径都返回拒绝决策（fail-closed）。
        """
        # 1. 基本字段校验
        invalid_reason = self._validate_request(request)
        if invalid_reason is not None:
            return deny_decision(invalid_reason)

        actor_roles = list(request.get("actor_roles") or [])

        # 2. ADMIN 短路放行
        if ADMIN_ROLE in actor_roles:
            return allow_decision(reason=REASON_ADMIN_DEFAULT_ALLOW)

        # 3. 加载并排序启用的策略
        try:
            policies = self._store.list_policies()
        except Exception:  # noqa: BLE001 —— 存储异常 fail-closed
            return deny_decision(REASON_POLICY_ERROR)

        enabled = [p for p in policies if p.get("enabled", True)]
        enabled.sort(key=lambda p: p.get("priority", 0), reverse=True)

        # 4. 逐策略、逐规则匹配
        for policy in enabled:
            rules = policy.get("rules") or []
            for rule in rules:
                try:
                    matched = rule_matches(rule, request)
                except Exception:  # noqa: BLE001 —— 单条规则异常跳过
                    continue
                if matched:
                    decision = rule.get("decision") or {}
                    return self._normalize_decision(decision)

        # 5. 默认拒绝
        return deny_decision(REASON_DEFAULT_DENY)

    async def evaluate_batch(
        self, requests: list[CapabilityRequest]
    ) -> list[CapabilityDecision]:
        """批量评估；顺序与 ``requests`` 一致。

        单条请求异常不影响其他请求；异常请求返回 ``POLICY_ERROR`` 拒绝。
        """
        if not isinstance(requests, list):
            return []
        results: list[CapabilityDecision] = []
        for req in requests:
            try:
                decision = await self.evaluate(req)
            except Exception:  # noqa: BLE001 —— 单条异常不影响其他
                decision = deny_decision(REASON_POLICY_ERROR)
            results.append(decision)
        return results

    # ------------------------------------------------------------------ #
    # 策略管理（仅 ADMIN）
    # ------------------------------------------------------------------ #

    async def set_policy(
        self,
        policy_id: str,
        rules: list[PolicyRule],
        *,
        actor_id: str,
        name: str | None = None,
        priority: int = 0,
        enabled: bool = True,
    ) -> CapabilityPolicy:
        """新增或更新能力策略；仅 ADMIN 可调用。

        :param policy_id: 策略稳定 ID；
        :param rules: 规则列表；
        :param actor_id: 调用者 user_id；本服务假定调用方已在外层通过
            Casbin ``require(actor, "write", "capability_policies")``；
            本服务仅断言 ``actor_id`` 非空，避免匿名写入。
        :param name: 策略名，默认与 ``policy_id`` 相同；
        :param priority: 优先级，默认 0；
        :param enabled: 是否启用，默认 True。
        :returns: 写入后的 ``CapabilityPolicy`` 视图。
        :raises PermissionDeniedError: ``actor_id`` 为空。
        :raises ArgumentError: ``policy_id`` 或 ``rules`` 非法。
        """
        if not isinstance(actor_id, str) or not actor_id:
            raise PermissionDeniedError(
                "set_policy 需要有效 actor_id",
                context={"operation": "set_policy"},
            )
        if not isinstance(policy_id, str) or not policy_id:
            raise ArgumentError(
                "policy_id 不能为空",
                context={"field": "policy_id"},
            )
        if not isinstance(rules, list) or not rules:
            raise ArgumentError(
                "rules 必须是非空列表",
                context={"field": "rules"},
            )
        # 校验每条 rule 结构
        for idx, rule in enumerate(rules):
            self._validate_rule(rule, idx)

        policy_name = name if name else policy_id
        return self._store.upsert_policy(
            policy_id=policy_id,
            name=policy_name,
            rules=list(rules),
            priority=int(priority),
            enabled=bool(enabled),
            created_by=actor_id,
        )

    async def get_policy(self, policy_id: str) -> CapabilityPolicy:
        """按 ID 获取策略；不存在抛 ``NotFoundError``。

        为保持接口简洁，本方法返回 ``CapabilityPolicy``；若需含审计字段
        （``created_by`` / ``created_at`` / ``version_no``），调用方应直接
        使用 ``SqliteCapabilityPolicyRepository.get_record``。
        """
        from maf_domain.errors import NotFoundError

        if not isinstance(policy_id, str) or not policy_id:
            raise ArgumentError(
                "policy_id 不能为空",
                context={"field": "policy_id"},
            )
        policy = self._store.get_policy(policy_id)
        if policy is None:
            raise NotFoundError(
                f"能力策略不存在: {policy_id!r}",
                context={"policy_id": policy_id},
            )
        return policy

    async def list_policies(self) -> list[CapabilityPolicy]:
        """列出全部策略（含禁用），按 priority 降序、policy_id 升序。"""
        policies = self._store.list_policies()
        policies.sort(
            key=lambda p: (-p.get("priority", 0), p.get("policy_id", ""))
        )
        return policies

    async def delete_policy(
        self, policy_id: str, *, actor_id: str
    ) -> bool:
        """删除策略；仅 ADMIN 可调用。返回是否删除成功。"""
        if not isinstance(actor_id, str) or not actor_id:
            raise PermissionDeniedError(
                "delete_policy 需要有效 actor_id",
                context={"operation": "delete_policy"},
            )
        if not isinstance(policy_id, str) or not policy_id:
            raise ArgumentError(
                "policy_id 不能为空",
                context={"field": "policy_id"},
            )
        return self._store.delete_policy(policy_id)

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _validate_request(request: CapabilityRequest) -> str | None:
        """校验请求基本字段；返回 reason_code 或 ``None`` 表示通过。"""
        if not isinstance(request, dict):
            return REASON_INVALID_REQUEST
        actor_id = request.get("actor_id")
        if not isinstance(actor_id, str) or not actor_id:
            return REASON_INVALID_REQUEST
        roles = request.get("actor_roles")
        if not isinstance(roles, list):
            return REASON_INVALID_REQUEST
        # 角色列表可为空（无角色用户），但元素必须是字符串
        for r in roles:
            if not isinstance(r, str):
                return REASON_INVALID_REQUEST
        cap_type = request.get("capability_type")
        if not isinstance(cap_type, str) or not cap_type:
            return REASON_INVALID_REQUEST
        if cap_type not in ALL_CAPABILITY_TYPES:
            return REASON_INVALID_REQUEST
        cap_name = request.get("capability_name")
        if not isinstance(cap_name, str) or not cap_name:
            return REASON_INVALID_REQUEST
        # capability_version 可空
        version = request.get("capability_version")
        if version is not None and not isinstance(version, str):
            return REASON_INVALID_REQUEST
        # context 必须是 dict（可空 dict）
        ctx = request.get("context")
        if ctx is not None and not isinstance(ctx, dict):
            return REASON_INVALID_REQUEST
        return None

    @staticmethod
    def _validate_rule(rule: PolicyRule, idx: int) -> None:
        """校验单条规则结构。"""
        if not isinstance(rule, dict):
            raise ArgumentError(
                f"rules[{idx}] 必须是 dict",
                context={"index": idx},
            )
        match = rule.get("match")
        if not isinstance(match, dict):
            raise ArgumentError(
                f"rules[{idx}].match 必须是 dict",
                context={"index": idx},
            )
        decision = rule.get("decision")
        if not isinstance(decision, dict):
            raise ArgumentError(
                f"rules[{idx}].decision 必须是 dict",
                context={"index": idx},
            )
        if not isinstance(decision.get("allowed"), bool):
            raise ArgumentError(
                f"rules[{idx}].decision.allowed 必须是 bool",
                context={"index": idx},
            )
        if not isinstance(decision.get("reason"), str) or not decision.get("reason"):
            raise ArgumentError(
                f"rules[{idx}].decision.reason 必须是非空 str",
                context={"index": idx},
            )
        conditions = decision.get("conditions")
        if conditions is not None and not isinstance(conditions, list):
            raise ArgumentError(
                f"rules[{idx}].decision.conditions 必须是 list",
                context={"index": idx},
            )
        # match.capability_type 校验
        cap_type = match.get("capability_type")
        if cap_type is not None and (
            not isinstance(cap_type, str)
            or (cap_type and cap_type not in ALL_CAPABILITY_TYPES)
        ):
            raise ArgumentError(
                f"rules[{idx}].match.capability_type 非法: {cap_type!r}",
                context={"index": idx, "value": cap_type},
            )

    @staticmethod
    def _normalize_decision(decision: Mapping[str, Any]) -> CapabilityDecision:
        """将规则中的 decision 规范化为 ``CapabilityDecision``。"""
        allowed = bool(decision.get("allowed", False))
        reason = decision.get("reason") or (
            REASON_POLICY_ALLOWED if allowed else REASON_POLICY_DENIED
        )
        conditions = decision.get("conditions")
        if not isinstance(conditions, list):
            conditions = []
        else:
            conditions = [str(c) for c in conditions]
        return CapabilityDecision(
            allowed=allowed,
            reason=str(reason),
            conditions=conditions,
        )


# --------------------------------------------------------------------------- #
# 工厂函数
# --------------------------------------------------------------------------- #


def build_default_capability_policy_service() -> CapabilityPolicyServiceImpl:
    """构造默认 ``CapabilityPolicyServiceImpl``（含默认策略）。"""
    return CapabilityPolicyServiceImpl(seed_defaults=True)


def build_empty_capability_policy_service() -> CapabilityPolicyServiceImpl:
    """构造不植入默认策略的 ``CapabilityPolicyServiceImpl``。

    供测试验证「无策略时默认拒绝」语义；生产代码应使用
    ``build_default_capability_policy_service``。
    """
    return CapabilityPolicyServiceImpl(seed_defaults=False)


__all__ = [
    "ADMIN_ROLE",
    "CapabilityPolicyServiceImpl",
    "build_default_capability_policy_service",
    "build_empty_capability_policy_service",
]
