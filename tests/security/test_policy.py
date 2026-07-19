"""TASK-050 安全测试：CapabilityPolicy 引擎。

验收标准（来自 TASK-050 任务文档与范围清单）：
1. ``CapabilityPolicyService`` 实现
   ``evaluate`` / ``evaluate_batch`` / ``set_policy`` / ``get_policy`` /
   ``list_policies``；
2. **默认拒绝**：未匹配任何策略时返回 ``allowed=False``；
3. **ADMIN 默认允许所有能力**；
4. 策略可配置（优先级、启用/禁用）；
5. 条件附加（``conditions``）正确传递；
6. 不破坏现有 RBAC 测试（``test_rbac.py``）。

测试范围：
- ``packages/policy/src/maf_policy/capability.py``：数据结构、匹配算法、
  默认策略、内存存储、SQLite 仓库。
- ``apps/server/src/maf_server/gateway/policy/capability_policy.py``：
  ``CapabilityPolicyServiceImpl`` 具体实现。
- 不测试 Tool 执行（TASK-051 范围）、不测试 HTTP 路由。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from maf_domain.errors import ArgumentError, NotFoundError, PermissionDeniedError
from maf_policy import (
    ALL_CAPABILITY_TYPES,
    CAPABILITY_POLICIES_SCHEMA_SQL,
    CAPABILITY_TYPE_MODEL,
    CAPABILITY_TYPE_SKILL,
    CAPABILITY_TYPE_TOOL,
    DEFAULT_ADMIN_ALLOW_POLICY_ID,
    REASON_ADMIN_DEFAULT_ALLOW,
    REASON_DEFAULT_DENY,
    REASON_INVALID_REQUEST,
    REASON_POLICY_ALLOWED,
    REASON_POLICY_DENIED,
    REASON_POLICY_ERROR,
    CapabilityDecision,
    CapabilityMatch,
    CapabilityPolicy,
    CapabilityPolicyRecord,
    CapabilityRequest,
    InMemoryCapabilityPolicyStore,
    PolicyRule,
    SqliteCapabilityPolicyRepository,
    allow_decision,
    build_default_capability_policies,
    deny_decision,
    rule_matches,
    seed_default_policies,
)
from maf_server.config import ServerSettings
from maf_server.core.database import Database
from maf_server.gateway.policy import (
    CapabilityPolicyServiceImpl,
    build_default_capability_policy_service,
    build_empty_capability_policy_service,
)

_SECRET_PLAINTEXT = "test-secret-for-policy-task-050"


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #


def _make_request(
    *,
    actor_id: str = "user-1",
    actor_roles: list[str] | None = None,
    capability_type: str = CAPABILITY_TYPE_TOOL,
    capability_name: str = "filesystem.read",
    capability_version: str | None = "1.0.0",
    context: dict[str, Any] | None = None,
) -> CapabilityRequest:
    """构造测试用 CapabilityRequest。"""
    return CapabilityRequest(
        actor_id=actor_id,
        actor_roles=list(actor_roles) if actor_roles is not None else [],
        capability_type=capability_type,
        capability_name=capability_name,
        capability_version=capability_version,
        context=dict(context) if context else {},
    )


def _make_rule(
    *,
    roles: list[str] | None = None,
    cap_type: str | None = None,
    cap_name: str = "*",
    cap_version: str | None = None,
    allowed: bool = True,
    reason: str = REASON_POLICY_ALLOWED,
    conditions: list[str] | None = None,
) -> PolicyRule:
    """构造测试用 PolicyRule。"""
    return PolicyRule(
        match=CapabilityMatch(
            actor_roles=roles,
            capability_type=cap_type,
            capability_name=cap_name,
            capability_version=cap_version,
        ),
        decision=CapabilityDecision(
            allowed=allowed,
            reason=reason,
            conditions=list(conditions) if conditions else [],
        ),
    )


def _make_settings(tmp_path: Path) -> ServerSettings:
    """构造测试用 ServerSettings（用于 SQLite 仓库测试）。"""
    kwargs: dict[str, object] = dict(
        organization_id="org-001",
        business_db_path=Path("maf.db"),
        checkpointer_db_path=Path("checkpoints.db"),
        artifact_root=Path("artifacts"),
        workspace_root=Path("workspaces"),
        git_repo_root=tmp_path / "repo",
        public_base_url="http://localhost:8000",
        secret_key=_SECRET_PLAINTEXT,
        data_dir=tmp_path,
        _env_file=None,
    )
    return ServerSettings(**kwargs)


# --------------------------------------------------------------------------- #
# 数据结构与匹配算法测试
# --------------------------------------------------------------------------- #


class TestCapabilityTypes:
    """能力类型常量与 TypedDict 字段。"""

    def test_all_capability_types_contains_three_values(self) -> None:
        """能力类型恰好为 tool/model/skill。"""
        assert ALL_CAPABILITY_TYPES == {"tool", "model", "skill"}

    def test_default_policies_include_admin_allow(self) -> None:
        """默认策略包含 ADMIN 全允许。"""
        defaults = build_default_capability_policies()
        ids = [p["policy_id"] for p in defaults]
        assert DEFAULT_ADMIN_ALLOW_POLICY_ID in ids

    def test_default_admin_allow_policy_has_high_priority(self) -> None:
        """ADMIN 全允许策略优先级最高。"""
        defaults = build_default_capability_policies()
        admin_policy = next(
            p for p in defaults if p["policy_id"] == DEFAULT_ADMIN_ALLOW_POLICY_ID
        )
        assert admin_policy["priority"] >= 100
        assert admin_policy["enabled"] is True
        # 含至少一条 ADMIN 规则
        admin_rules = [
            r for r in admin_policy["rules"]
            if "ADMIN" in (r.get("match", {}).get("actor_roles") or [])
        ]
        assert len(admin_rules) >= 1
        assert admin_rules[0]["decision"]["allowed"] is True


class TestRuleMatching:
    """``rule_matches`` 匹配算法。"""

    def test_role_wildcard_matches_any_role(self) -> None:
        """``actor_roles=None`` 通配任意角色。"""
        rule = _make_rule(roles=None, cap_name="*")
        req = _make_request(actor_roles=["OBSERVER"], capability_name="x")
        assert rule_matches(rule, req) is True

    def test_role_empty_list_matches_any_role(self) -> None:
        """``actor_roles=[]`` 也通配任意角色。"""
        rule = _make_rule(roles=[], cap_name="*")
        req = _make_request(actor_roles=["OBSERVER"], capability_name="x")
        assert rule_matches(rule, req) is True

    def test_role_intersection_matches(self) -> None:
        """规则角色与请求角色有交集即匹配。"""
        rule = _make_rule(roles=["OWNER", "APPROVER"], cap_name="*")
        req = _make_request(actor_roles=["OBSERVER", "APPROVER"], capability_name="x")
        assert rule_matches(rule, req) is True

    def test_role_no_intersection_not_matches(self) -> None:
        """规则角色与请求角色无交集不匹配。"""
        rule = _make_rule(roles=["OWNER"], cap_name="*")
        req = _make_request(actor_roles=["OBSERVER"], capability_name="x")
        assert rule_matches(rule, req) is False

    def test_capability_type_must_match(self) -> None:
        """能力类型必须相等（除非规则为 None）。"""
        rule = _make_rule(roles=None, cap_type=CAPABILITY_TYPE_TOOL, cap_name="*")
        assert rule_matches(rule, _make_request(capability_type="tool")) is True
        assert rule_matches(rule, _make_request(capability_type="model")) is False

    def test_capability_type_none_wildcard(self) -> None:
        """``capability_type=None`` 通配所有类型。"""
        rule = _make_rule(roles=None, cap_type=None, cap_name="*")
        for t in ("tool", "model", "skill"):
            assert rule_matches(rule, _make_request(capability_type=t)) is True

    def test_name_fnmatch_pattern(self) -> None:
        """capability_name 支持 fnmatch 模式。"""
        rule = _make_rule(roles=None, cap_name="filesystem.*")
        assert rule_matches(rule, _make_request(capability_name="filesystem.read")) is True
        assert rule_matches(rule, _make_request(capability_name="filesystem.write")) is True
        assert rule_matches(rule, _make_request(capability_name="shell.exec")) is False

    def test_name_question_mark_pattern(self) -> None:
        """``?`` 匹配单个字符。"""
        rule = _make_rule(roles=None, cap_name="tool-?.run")
        assert rule_matches(rule, _make_request(capability_name="tool-a.run")) is True
        assert rule_matches(rule, _make_request(capability_name="tool-ab.run")) is False

    def test_version_exact_match(self) -> None:
        """版本精确匹配。"""
        rule = _make_rule(roles=None, cap_name="*", cap_version="1.0.0")
        assert rule_matches(rule, _make_request(capability_version="1.0.0")) is True
        assert rule_matches(rule, _make_request(capability_version="2.0.0")) is False

    def test_version_none_in_rule_wildcard(self) -> None:
        """规则 ``capability_version=None`` 通配任意版本。"""
        rule = _make_rule(roles=None, cap_name="*", cap_version=None)
        assert rule_matches(rule, _make_request(capability_version="1.0.0")) is True
        assert rule_matches(rule, _make_request(capability_version=None)) is True

    def test_version_none_in_request_only_matches_wildcard_rule(self) -> None:
        """请求未指定版本时，规则指定具体版本则不匹配。"""
        rule = _make_rule(roles=None, cap_name="*", cap_version="1.0.0")
        req = _make_request(capability_version=None)
        assert rule_matches(rule, req) is False

    def test_version_fnmatch_pattern(self) -> None:
        """版本也支持 fnmatch 模式。"""
        rule = _make_rule(roles=None, cap_name="*", cap_version="1.*")
        assert rule_matches(rule, _make_request(capability_version="1.0.0")) is True
        assert rule_matches(rule, _make_request(capability_version="1.2.3")) is True
        assert rule_matches(rule, _make_request(capability_version="2.0.0")) is False


# --------------------------------------------------------------------------- #
# 内存存储测试
# --------------------------------------------------------------------------- #


class TestInMemoryCapabilityPolicyStore:
    """``InMemoryCapabilityPolicyStore`` CRUD。"""

    def test_upsert_and_get_policy(self) -> None:
        store = InMemoryCapabilityPolicyStore()
        rule = _make_rule(roles=["DESIGNER"], cap_name="*")
        policy = store.upsert_policy(
            policy_id="p1",
            name="designer allow",
            rules=[rule],
            priority=10,
            enabled=True,
            created_by="admin-1",
        )
        assert policy["policy_id"] == "p1"
        assert policy["name"] == "designer allow"
        assert policy["priority"] == 10
        assert policy["enabled"] is True
        assert len(policy["rules"]) == 1

        fetched = store.get_policy("p1")
        assert fetched is not None
        assert fetched["policy_id"] == "p1"

    def test_get_policy_returns_none_if_missing(self) -> None:
        store = InMemoryCapabilityPolicyStore()
        assert store.get_policy("nonexistent") is None

    def test_list_policies_returns_all(self) -> None:
        store = InMemoryCapabilityPolicyStore()
        store.upsert_policy("p1", "n1", [_make_rule()], 1, True, created_by="a")
        store.upsert_policy("p2", "n2", [_make_rule()], 2, False, created_by="a")
        policies = store.list_policies()
        assert len(policies) == 2
        ids = {p["policy_id"] for p in policies}
        assert ids == {"p1", "p2"}

    def test_upsert_overwrites_existing(self) -> None:
        store = InMemoryCapabilityPolicyStore()
        store.upsert_policy("p1", "old", [_make_rule()], 1, True, created_by="a")
        store.upsert_policy("p1", "new", [_make_rule(), _make_rule()], 5, False, created_by="b")
        policy = store.get_policy("p1")
        assert policy is not None
        assert policy["name"] == "new"
        assert policy["priority"] == 5
        assert policy["enabled"] is False
        assert len(policy["rules"]) == 2

    def test_delete_policy(self) -> None:
        store = InMemoryCapabilityPolicyStore()
        store.upsert_policy("p1", "n1", [_make_rule()], 1, True, created_by="a")
        assert store.delete_policy("p1") is True
        assert store.get_policy("p1") is None
        # 再次删除返回 False
        assert store.delete_policy("p1") is False

    def test_seed_default_policies_idempotent(self) -> None:
        """植入默认策略幂等：不覆盖已有同 ID 策略。"""
        store = InMemoryCapabilityPolicyStore()
        seed_default_policies(store)
        first = store.get_policy(DEFAULT_ADMIN_ALLOW_POLICY_ID)
        assert first is not None
        # 手动修改后再 seed，不应被覆盖
        store.upsert_policy(
            DEFAULT_ADMIN_ALLOW_POLICY_ID,
            "custom",
            [_make_rule()],
            999,
            False,
            created_by="admin",
        )
        seed_default_policies(store)
        second = store.get_policy(DEFAULT_ADMIN_ALLOW_POLICY_ID)
        assert second is not None
        assert second["name"] == "custom"
        assert second["priority"] == 999


# --------------------------------------------------------------------------- #
# CapabilityPolicyServiceImpl.evaluate 测试
# --------------------------------------------------------------------------- #


class TestEvaluateAllowDeny:
    """evaluate 允许/拒绝基础用例。"""

    @pytest.mark.asyncio
    async def test_admin_default_allow_all(self) -> None:
        """ADMIN 角色默认允许所有能力（无需任何策略）。"""
        svc = build_empty_capability_policy_service()
        # 即使没有任何策略，ADMIN 也能用任何能力
        for cap_type in ("tool", "model", "skill"):
            req = _make_request(
                actor_roles=["ADMIN"],
                capability_type=cap_type,
                capability_name="any.thing",
            )
            decision = await svc.evaluate(req)
            assert decision["allowed"] is True
            assert decision["reason"] == REASON_ADMIN_DEFAULT_ALLOW

    @pytest.mark.asyncio
    async def test_admin_allow_even_when_store_empty(self) -> None:
        """空存储下 ADMIN 仍放行（短路逻辑）。"""
        svc = build_empty_capability_policy_service()
        req = _make_request(
            actor_roles=["ADMIN"],
            capability_type="tool",
            capability_name="filesystem.write",
        )
        decision = await svc.evaluate(req)
        assert decision["allowed"] is True

    @pytest.mark.asyncio
    async def test_default_deny_for_non_admin_without_policy(self) -> None:
        """非 ADMIN 且无策略匹配时默认拒绝。"""
        svc = build_empty_capability_policy_service()
        req = _make_request(
            actor_roles=["OBSERVER"],
            capability_type="tool",
            capability_name="filesystem.read",
        )
        decision = await svc.evaluate(req)
        assert decision["allowed"] is False
        assert decision["reason"] == REASON_DEFAULT_DENY

    @pytest.mark.asyncio
    async def test_default_deny_for_no_roles(self) -> None:
        """无角色用户默认拒绝。"""
        svc = build_default_capability_policy_service()
        req = _make_request(actor_roles=[], capability_name="filesystem.read")
        decision = await svc.evaluate(req)
        assert decision["allowed"] is False
        assert decision["reason"] == REASON_DEFAULT_DENY

    @pytest.mark.asyncio
    async def test_policy_allows_specific_tool(self) -> None:
        """显式策略允许特定 Tool。"""
        svc = build_empty_capability_policy_service()
        rule = _make_rule(
            roles=["DESIGNER"],
            cap_type=CAPABILITY_TYPE_TOOL,
            cap_name="filesystem.read",
            allowed=True,
        )
        await svc.set_policy(
            "designer-fs-read",
            [rule],
            actor_id="admin-1",
            priority=10,
        )
        req = _make_request(
            actor_roles=["DESIGNER"],
            capability_type="tool",
            capability_name="filesystem.read",
        )
        decision = await svc.evaluate(req)
        assert decision["allowed"] is True
        assert decision["reason"] == REASON_POLICY_ALLOWED

    @pytest.mark.asyncio
    async def test_policy_denies_specific_tool(self) -> None:
        """显式策略拒绝特定 Tool。"""
        svc = build_empty_capability_policy_service()
        rule = _make_rule(
            roles=["DESIGNER"],
            cap_type=CAPABILITY_TYPE_TOOL,
            cap_name="shell.exec",
            allowed=False,
            reason=REASON_POLICY_DENIED,
        )
        await svc.set_policy(
            "designer-shell-deny",
            [rule],
            actor_id="admin-1",
        )
        req = _make_request(
            actor_roles=["DESIGNER"],
            capability_type="tool",
            capability_name="shell.exec",
        )
        decision = await svc.evaluate(req)
        assert decision["allowed"] is False
        assert decision["reason"] == REASON_POLICY_DENIED

    @pytest.mark.asyncio
    async def test_policy_with_conditions(self) -> None:
        """允许决策携带 conditions。"""
        svc = build_empty_capability_policy_service()
        rule = _make_rule(
            roles=["OWNER"],
            cap_type=CAPABILITY_TYPE_MODEL,
            cap_name="gpt-4",
            allowed=True,
            conditions=["rate_limit:100/hour", "memory_limit:512m"],
        )
        await svc.set_policy(
            "owner-gpt4",
            [rule],
            actor_id="admin-1",
        )
        req = _make_request(
            actor_roles=["OWNER"],
            capability_type="model",
            capability_name="gpt-4",
        )
        decision = await svc.evaluate(req)
        assert decision["allowed"] is True
        assert "rate_limit:100/hour" in decision["conditions"]
        assert "memory_limit:512m" in decision["conditions"]

    @pytest.mark.asyncio
    async def test_pattern_match_allows_tool_family(self) -> None:
        """fnmatch 模式匹配工具族。"""
        svc = build_empty_capability_policy_service()
        rule = _make_rule(
            roles=["DESIGNER"],
            cap_type=CAPABILITY_TYPE_TOOL,
            cap_name="filesystem.*",
        )
        await svc.set_policy("designer-fs", [rule], actor_id="admin-1")
        # filesystem.read / filesystem.write 都允许
        for name in ("filesystem.read", "filesystem.write", "filesystem.list"):
            req = _make_request(
                actor_roles=["DESIGNER"],
                capability_type="tool",
                capability_name=name,
            )
            decision = await svc.evaluate(req)
            assert decision["allowed"] is True, f"应允许 {name}"
        # shell.exec 不在模式内 → 默认拒绝
        req = _make_request(
            actor_roles=["DESIGNER"],
            capability_type="tool",
            capability_name="shell.exec",
        )
        decision = await svc.evaluate(req)
        assert decision["allowed"] is False
        assert decision["reason"] == REASON_DEFAULT_DENY


# --------------------------------------------------------------------------- #
# 默认拒绝与无效请求测试
# --------------------------------------------------------------------------- #


class TestDefaultDenyAndInvalidRequest:
    """默认拒绝与无效请求处理。"""

    @pytest.mark.asyncio
    async def test_invalid_request_missing_actor_id(self) -> None:
        """actor_id 缺失返回 INVALID_REQUEST。"""
        svc = build_default_capability_policy_service()
        req = _make_request(actor_id="", actor_roles=["ADMIN"])
        decision = await svc.evaluate(req)
        assert decision["allowed"] is False
        assert decision["reason"] == REASON_INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_invalid_request_missing_capability_type(self) -> None:
        """capability_type 缺失返回 INVALID_REQUEST。"""
        svc = build_default_capability_policy_service()
        req: CapabilityRequest = CapabilityRequest(
            actor_id="u1",
            actor_roles=["ADMIN"],
            capability_type="",
            capability_name="x",
            capability_version=None,
            context={},
        )
        decision = await svc.evaluate(req)
        assert decision["allowed"] is False
        assert decision["reason"] == REASON_INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_invalid_request_unknown_capability_type(self) -> None:
        """capability_type 取值非法返回 INVALID_REQUEST。"""
        svc = build_default_capability_policy_service()
        req = _make_request(
            actor_roles=["ADMIN"],
            capability_type="unknown",
            capability_name="x",
        )
        decision = await svc.evaluate(req)
        assert decision["allowed"] is False
        assert decision["reason"] == REASON_INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_invalid_request_missing_capability_name(self) -> None:
        """capability_name 缺失返回 INVALID_REQUEST。"""
        svc = build_default_capability_policy_service()
        req = _make_request(
            actor_roles=["ADMIN"],
            capability_name="",
        )
        decision = await svc.evaluate(req)
        assert decision["allowed"] is False
        assert decision["reason"] == REASON_INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_invalid_request_non_dict(self) -> None:
        """非 dict 请求返回 INVALID_REQUEST。"""
        svc = build_default_capability_policy_service()
        decision = await svc.evaluate("not-a-dict")  # type: ignore[arg-type]
        assert decision["allowed"] is False
        assert decision["reason"] == REASON_INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_invalid_request_roles_not_list(self) -> None:
        """actor_roles 不是 list 返回 INVALID_REQUEST。"""
        svc = build_default_capability_policy_service()
        req: CapabilityRequest = CapabilityRequest(
            actor_id="u1",
            actor_roles="ADMIN",  # type: ignore[list-item]
            capability_type="tool",
            capability_name="x",
            capability_version=None,
            context={},
        )
        decision = await svc.evaluate(req)
        assert decision["allowed"] is False
        assert decision["reason"] == REASON_INVALID_REQUEST

    @pytest.mark.asyncio
    async def test_no_matching_policy_denies(self) -> None:
        """无匹配策略时拒绝。"""
        svc = build_empty_capability_policy_service()
        # 设置一个针对 DESIGNER 的策略，但请求来自 OBSERVER
        rule = _make_rule(roles=["DESIGNER"], cap_name="*")
        await svc.set_policy("designer-only", [rule], actor_id="admin-1")
        req = _make_request(actor_roles=["OBSERVER"], capability_name="x")
        decision = await svc.evaluate(req)
        assert decision["allowed"] is False
        assert decision["reason"] == REASON_DEFAULT_DENY


# --------------------------------------------------------------------------- #
# 批量评估测试
# --------------------------------------------------------------------------- #


class TestEvaluateBatch:
    """``evaluate_batch`` 批量评估。"""

    @pytest.mark.asyncio
    async def test_batch_returns_one_decision_per_request(self) -> None:
        """批量返回数量与请求一致。"""
        svc = build_default_capability_policy_service()
        requests = [
            _make_request(actor_roles=["ADMIN"], capability_name="a"),
            _make_request(actor_roles=["OBSERVER"], capability_name="b"),
            _make_request(actor_roles=["ADMIN"], capability_name="c"),
        ]
        decisions = await svc.evaluate_batch(requests)
        assert len(decisions) == 3
        assert decisions[0]["allowed"] is True  # ADMIN
        assert decisions[1]["allowed"] is False  # OBSERVER 默认拒绝
        assert decisions[2]["allowed"] is True  # ADMIN

    @pytest.mark.asyncio
    async def test_batch_empty_requests_returns_empty(self) -> None:
        """空请求列表返回空决策列表。"""
        svc = build_default_capability_policy_service()
        decisions = await svc.evaluate_batch([])
        assert decisions == []

    @pytest.mark.asyncio
    async def test_batch_non_list_returns_empty(self) -> None:
        """非 list 入参返回空列表。"""
        svc = build_default_capability_policy_service()
        decisions = await svc.evaluate_batch("not-a-list")  # type: ignore[arg-type]
        assert decisions == []

    @pytest.mark.asyncio
    async def test_batch_preserves_order(self) -> None:
        """批量结果顺序与请求一致。"""
        svc = build_empty_capability_policy_service()
        # 设置策略：DESIGNER 允许 tool.a，OBSERVER 不允许
        rule_d = _make_rule(roles=["DESIGNER"], cap_name="tool.a")
        await svc.set_policy("d", [rule_d], actor_id="admin-1")
        requests = [
            _make_request(actor_roles=["DESIGNER"], capability_name="tool.a"),
            _make_request(actor_roles=["OBSERVER"], capability_name="tool.a"),
            _make_request(actor_roles=["DESIGNER"], capability_name="tool.b"),
        ]
        decisions = await svc.evaluate_batch(requests)
        assert decisions[0]["allowed"] is True
        assert decisions[1]["allowed"] is False
        assert decisions[2]["allowed"] is False


# --------------------------------------------------------------------------- #
# 策略管理测试
# --------------------------------------------------------------------------- #


class TestPolicyManagement:
    """``set_policy`` / ``get_policy`` / ``list_policies`` / ``delete_policy``。"""

    @pytest.mark.asyncio
    async def test_set_policy_returns_policy_view(self) -> None:
        svc = build_empty_capability_policy_service()
        rule = _make_rule(roles=["OWNER"], cap_name="*")
        policy = await svc.set_policy(
            "owner-all",
            [rule],
            actor_id="admin-1",
            name="Owner allow all",
            priority=50,
            enabled=True,
        )
        assert policy["policy_id"] == "owner-all"
        assert policy["name"] == "Owner allow all"
        assert policy["priority"] == 50
        assert policy["enabled"] is True
        assert len(policy["rules"]) == 1

    @pytest.mark.asyncio
    async def test_set_policy_default_name_equals_id(self) -> None:
        """未传 name 时使用 policy_id。"""
        svc = build_empty_capability_policy_service()
        policy = await svc.set_policy(
            "p1", [_make_rule()], actor_id="admin-1"
        )
        assert policy["name"] == "p1"

    @pytest.mark.asyncio
    async def test_set_policy_requires_actor_id(self) -> None:
        """set_policy 无 actor_id 抛 PermissionDeniedError。"""
        svc = build_empty_capability_policy_service()
        with pytest.raises(PermissionDeniedError):
            await svc.set_policy(
                "p1", [_make_rule()], actor_id=""
            )

    @pytest.mark.asyncio
    async def test_set_policy_rejects_empty_policy_id(self) -> None:
        """空 policy_id 抛 ArgumentError。"""
        svc = build_empty_capability_policy_service()
        with pytest.raises(ArgumentError):
            await svc.set_policy(
                "", [_make_rule()], actor_id="admin-1"
            )

    @pytest.mark.asyncio
    async def test_set_policy_rejects_empty_rules(self) -> None:
        """空 rules 抛 ArgumentError。"""
        svc = build_empty_capability_policy_service()
        with pytest.raises(ArgumentError):
            await svc.set_policy(
                "p1", [], actor_id="admin-1"
            )

    @pytest.mark.asyncio
    async def test_set_policy_rejects_invalid_rule_structure(self) -> None:
        """rule 结构非法抛 ArgumentError。"""
        svc = build_empty_capability_policy_service()
        # rule 不是 dict
        with pytest.raises(ArgumentError):
            await svc.set_policy(
                "p1", ["not-a-dict"], actor_id="admin-1"  # type: ignore[list-item]
            )
        # decision.allowed 不是 bool
        bad_rule: PolicyRule = PolicyRule(
            match=CapabilityMatch(
                actor_roles=None,
                capability_type=None,
                capability_name="*",
                capability_version=None,
            ),
            decision=CapabilityDecision(
                allowed="yes",  # type: ignore[arg-type]
                reason="r",
                conditions=[],
            ),
        )
        with pytest.raises(ArgumentError):
            await svc.set_policy("p1", [bad_rule], actor_id="admin-1")

    @pytest.mark.asyncio
    async def test_set_policy_rejects_invalid_capability_type_in_rule(self) -> None:
        """rule.match.capability_type 取值非法抛 ArgumentError。"""
        svc = build_empty_capability_policy_service()
        bad_rule: PolicyRule = PolicyRule(
            match=CapabilityMatch(
                actor_roles=None,
                capability_type="unknown_type",
                capability_name="*",
                capability_version=None,
            ),
            decision=allow_decision(),
        )
        with pytest.raises(ArgumentError):
            await svc.set_policy("p1", [bad_rule], actor_id="admin-1")

    @pytest.mark.asyncio
    async def test_get_policy_returns_existing(self) -> None:
        svc = build_empty_capability_policy_service()
        await svc.set_policy(
            "p1", [_make_rule()], actor_id="admin-1"
        )
        policy = await svc.get_policy("p1")
        assert policy["policy_id"] == "p1"

    @pytest.mark.asyncio
    async def test_get_policy_raises_not_found(self) -> None:
        """不存在的策略抛 NotFoundError。"""
        svc = build_empty_capability_policy_service()
        with pytest.raises(NotFoundError):
            await svc.get_policy("nonexistent")

    @pytest.mark.asyncio
    async def test_get_policy_rejects_empty_id(self) -> None:
        """空 policy_id 抛 ArgumentError。"""
        svc = build_empty_capability_policy_service()
        with pytest.raises(ArgumentError):
            await svc.get_policy("")

    @pytest.mark.asyncio
    async def test_list_policies_sorted_by_priority_desc(self) -> None:
        """list_policies 按 priority 降序。"""
        svc = build_empty_capability_policy_service()
        await svc.set_policy("low", [_make_rule()], actor_id="a", priority=10)
        await svc.set_policy("high", [_make_rule()], actor_id="a", priority=100)
        await svc.set_policy("mid", [_make_rule()], actor_id="a", priority=50)
        policies = await svc.list_policies()
        priorities = [p["priority"] for p in policies]
        # 应为降序
        assert priorities == sorted(priorities, reverse=True)
        assert policies[0]["policy_id"] == "high"

    @pytest.mark.asyncio
    async def test_list_policies_includes_disabled(self) -> None:
        """list_policies 包含禁用的策略。"""
        svc = build_empty_capability_policy_service()
        await svc.set_policy(
            "enabled", [_make_rule()], actor_id="a", enabled=True
        )
        await svc.set_policy(
            "disabled", [_make_rule()], actor_id="a", enabled=False
        )
        policies = await svc.list_policies()
        ids = {p["policy_id"] for p in policies}
        assert ids == {"enabled", "disabled"}

    @pytest.mark.asyncio
    async def test_delete_policy(self) -> None:
        svc = build_empty_capability_policy_service()
        await svc.set_policy("p1", [_make_rule()], actor_id="a")
        assert await svc.delete_policy("p1", actor_id="a") is True
        with pytest.raises(NotFoundError):
            await svc.get_policy("p1")
        # 再次删除返回 False
        assert await svc.delete_policy("p1", actor_id="a") is False

    @pytest.mark.asyncio
    async def test_delete_policy_requires_actor_id(self) -> None:
        svc = build_empty_capability_policy_service()
        with pytest.raises(PermissionDeniedError):
            await svc.delete_policy("p1", actor_id="")


# --------------------------------------------------------------------------- #
# 策略优先级测试
# --------------------------------------------------------------------------- #


class TestPolicyPriority:
    """策略优先级：高优先级先匹配。"""

    @pytest.mark.asyncio
    async def test_higher_priority_policy_wins(self) -> None:
        """高优先级策略的决策生效。"""
        svc = build_empty_capability_policy_service()
        # 低优先级允许
        await svc.set_policy(
            "allow-low",
            [_make_rule(roles=["OWNER"], cap_name="tool.x", allowed=True)],
            actor_id="admin-1",
            priority=10,
        )
        # 高优先级拒绝
        await svc.set_policy(
            "deny-high",
            [_make_rule(roles=["OWNER"], cap_name="tool.x", allowed=False, reason=REASON_POLICY_DENIED)],
            actor_id="admin-1",
            priority=100,
        )
        req = _make_request(
            actor_roles=["OWNER"],
            capability_type="tool",
            capability_name="tool.x",
        )
        decision = await svc.evaluate(req)
        assert decision["allowed"] is False
        assert decision["reason"] == REASON_POLICY_DENIED

    @pytest.mark.asyncio
    async def test_first_matching_rule_in_policy_wins(self) -> None:
        """同一策略内首条命中规则生效。"""
        svc = build_empty_capability_policy_service()
        rules = [
            _make_rule(roles=["OWNER"], cap_name="tool.x", allowed=False, reason="DENIED_BY_FIRST"),
            _make_rule(roles=["OWNER"], cap_name="tool.x", allowed=True),
        ]
        await svc.set_policy("p1", rules, actor_id="admin-1")
        req = _make_request(actor_roles=["OWNER"], capability_name="tool.x")
        decision = await svc.evaluate(req)
        assert decision["allowed"] is False
        assert decision["reason"] == "DENIED_BY_FIRST"


# --------------------------------------------------------------------------- #
# 策略启用/禁用测试
# --------------------------------------------------------------------------- #


class TestPolicyEnabledDisabled:
    """禁用的策略不参与评估。"""

    @pytest.mark.asyncio
    async def test_disabled_policy_not_evaluated(self) -> None:
        """禁用的策略不生效。"""
        svc = build_empty_capability_policy_service()
        # 设置一个禁用的允许策略
        await svc.set_policy(
            "disabled-allow",
            [_make_rule(roles=["OWNER"], cap_name="tool.x", allowed=True)],
            actor_id="admin-1",
            enabled=False,
        )
        req = _make_request(
            actor_roles=["OWNER"],
            capability_type="tool",
            capability_name="tool.x",
        )
        decision = await svc.evaluate(req)
        # 策略被禁用 → 默认拒绝
        assert decision["allowed"] is False
        assert decision["reason"] == REASON_DEFAULT_DENY

    @pytest.mark.asyncio
    async def test_enable_policy_takes_effect(self) -> None:
        """重新启用策略后生效。"""
        svc = build_empty_capability_policy_service()
        # 先创建禁用策略
        await svc.set_policy(
            "p1",
            [_make_rule(roles=["OWNER"], cap_name="tool.x", allowed=True)],
            actor_id="admin-1",
            enabled=False,
        )
        req = _make_request(actor_roles=["OWNER"], capability_name="tool.x")
        assert (await svc.evaluate(req))["allowed"] is False

        # 启用策略
        await svc.set_policy(
            "p1",
            [_make_rule(roles=["OWNER"], cap_name="tool.x", allowed=True)],
            actor_id="admin-1",
            enabled=True,
        )
        assert (await svc.evaluate(req))["allowed"] is True


# --------------------------------------------------------------------------- #
# 默认策略集成测试
# --------------------------------------------------------------------------- #


class TestDefaultPolicies:
    """默认策略（ADMIN 全允许 + OBSERVER tool 列表只读）。"""

    @pytest.mark.asyncio
    async def test_default_service_has_admin_allow_policy(self) -> None:
        """默认服务包含 ADMIN 全允许策略。"""
        svc = build_default_capability_policy_service()
        policies = await svc.list_policies()
        ids = {p["policy_id"] for p in policies}
        assert DEFAULT_ADMIN_ALLOW_POLICY_ID in ids

    @pytest.mark.asyncio
    async def test_admin_request_uses_shortcut_not_policy(self) -> None:
        """ADMIN 请求走短路逻辑，reason 为 ADMIN_DEFAULT_ALLOW。"""
        svc = build_default_capability_policy_service()
        req = _make_request(actor_roles=["ADMIN"], capability_name="any")
        decision = await svc.evaluate(req)
        assert decision["allowed"] is True
        assert decision["reason"] == REASON_ADMIN_DEFAULT_ALLOW


# --------------------------------------------------------------------------- #
# SQLite 存储测试
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def policy_db(tmp_path: Path) -> Database:
    """已初始化并建好 capability_policies 表的 Database。

    ``Database.write_connection`` 上下文管理器在退出时自动 ``COMMIT``，
    故 fixture 内不需要手动 ``conn.commit()``。
    """
    settings = _make_settings(tmp_path)
    database = Database(settings)
    await database.initialize()
    async with database.write_connection() as conn:
        await conn.execute(CAPABILITY_POLICIES_SCHEMA_SQL)
    yield database
    await database.close()


class TestSqliteCapabilityPolicyRepository:
    """``SqliteCapabilityPolicyRepository`` CRUD。"""

    @pytest.mark.asyncio
    async def test_init_schema_creates_table(
        self, policy_db: Database
    ) -> None:
        """init_schema 创建 capability_policies 表。"""
        async with policy_db.read_connection() as conn:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='capability_policies'"
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        assert row[0] == "capability_policies"

    @pytest.mark.asyncio
    async def test_upsert_and_get_policy(self, policy_db: Database) -> None:
        repo = SqliteCapabilityPolicyRepository()
        rule = _make_rule(roles=["DESIGNER"], cap_name="filesystem.*")
        async with policy_db.write_connection() as conn:
            record = await repo.upsert_policy(
                conn,
                policy_id="p1",
                name="designer-fs",
                rules=[rule],
                priority=20,
                enabled=True,
                created_by="admin-1",
            )

        assert record["policy_id"] == "p1"
        assert record["version_no"] == 1
        assert record["created_by"] == "admin-1"
        assert record["priority"] == 20

        async with policy_db.read_connection() as conn:
            fetched = await repo.get_policy(conn, "p1")
            fetched_record = await repo.get_record(conn, "p1")

        assert fetched is not None
        assert fetched["policy_id"] == "p1"
        assert fetched["priority"] == 20
        assert len(fetched["rules"]) == 1

        assert fetched_record is not None
        assert fetched_record["version_no"] == 1

    @pytest.mark.asyncio
    async def test_upsert_increments_version_no(
        self, policy_db: Database
    ) -> None:
        """重复 upsert 自增 version_no。"""
        repo = SqliteCapabilityPolicyRepository()
        rule = _make_rule()

        async with policy_db.write_connection() as conn:
            r1 = await repo.upsert_policy(
                conn, "p1", "n1", [rule], 1, True, created_by="a"
            )
            r2 = await repo.upsert_policy(
                conn, "p1", "n2", [rule, rule], 5, False, created_by="b"
            )

        assert r1["version_no"] == 1
        assert r2["version_no"] == 2
        assert r2["name"] == "n2"
        assert r2["priority"] == 5
        assert r2["enabled"] is False
        # created_by 保留首次写入的值
        assert r2["created_by"] == "a"

    @pytest.mark.asyncio
    async def test_list_policies_orders_by_priority_desc(
        self, policy_db: Database
    ) -> None:
        repo = SqliteCapabilityPolicyRepository()
        async with policy_db.write_connection() as conn:
            await repo.upsert_policy(conn, "low", "n1", [_make_rule()], 10, True, created_by="a")
            await repo.upsert_policy(conn, "high", "n2", [_make_rule()], 100, True, created_by="a")
            await repo.upsert_policy(conn, "mid", "n3", [_make_rule()], 50, True, created_by="a")

        async with policy_db.read_connection() as conn:
            policies = await repo.list_policies(conn)

        assert len(policies) == 3
        assert policies[0]["policy_id"] == "high"
        assert policies[1]["policy_id"] == "mid"
        assert policies[2]["policy_id"] == "low"

    @pytest.mark.asyncio
    async def test_delete_policy(self, policy_db: Database) -> None:
        repo = SqliteCapabilityPolicyRepository()
        async with policy_db.write_connection() as conn:
            await repo.upsert_policy(conn, "p1", "n1", [_make_rule()], 1, True, created_by="a")
            deleted = await repo.delete_policy(conn, "p1")

        assert deleted is True
        async with policy_db.read_connection() as conn:
            assert await repo.get_policy(conn, "p1") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(
        self, policy_db: Database
    ) -> None:
        repo = SqliteCapabilityPolicyRepository()
        async with policy_db.write_connection() as conn:
            deleted = await repo.delete_policy(conn, "nonexistent")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_set_enabled_toggle(self, policy_db: Database) -> None:
        """set_enabled 切换启用状态。"""
        repo = SqliteCapabilityPolicyRepository()
        async with policy_db.write_connection() as conn:
            await repo.upsert_policy(conn, "p1", "n1", [_make_rule()], 1, True, created_by="a")
            affected = await repo.set_enabled(conn, "p1", False)

        assert affected is True
        async with policy_db.read_connection() as conn:
            policy = await repo.get_policy(conn, "p1")
        assert policy is not None
        assert policy["enabled"] is False

    @pytest.mark.asyncio
    async def test_rules_json_round_trip(self, policy_db: Database) -> None:
        """rules JSON 序列化/反序列化保真。"""
        repo = SqliteCapabilityPolicyRepository()
        rules = [
            _make_rule(
                roles=["DESIGNER", "OWNER"],
                cap_type=CAPABILITY_TYPE_TOOL,
                cap_name="filesystem.*",
                cap_version="1.*",
                allowed=True,
                conditions=["rate_limit:100/hour"],
            ),
            _make_rule(
                roles=None,
                cap_type=CAPABILITY_TYPE_MODEL,
                cap_name="gpt-4",
                allowed=False,
                reason="MODEL_RESTRICTED",
            ),
        ]
        async with policy_db.write_connection() as conn:
            await repo.upsert_policy(
                conn, "p1", "n1", rules, 10, True, created_by="a"
            )

        async with policy_db.read_connection() as conn:
            fetched = await repo.get_policy(conn, "p1")

        assert fetched is not None
        assert len(fetched["rules"]) == 2
        r0 = fetched["rules"][0]
        assert set(r0["match"]["actor_roles"] or []) == {"DESIGNER", "OWNER"}
        assert r0["match"]["capability_type"] == "tool"
        assert r0["match"]["capability_name"] == "filesystem.*"
        assert r0["decision"]["allowed"] is True
        assert "rate_limit:100/hour" in r0["decision"]["conditions"]

    @pytest.mark.asyncio
    async def test_corrupted_rules_json_returns_empty(
        self, policy_db: Database
    ) -> None:
        """rules 字段损坏时返回空规则列表（fail-closed）。"""
        async with policy_db.write_connection() as conn:
            await conn.execute(
                "INSERT INTO capability_policies "
                "(policy_id, name, rules, priority, enabled, created_by, "
                " created_at, version_no) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                ("p1", "n1", "not-valid-json", 1, 1, "a", "2024-01-01T00:00:00Z"),
            )

        repo = SqliteCapabilityPolicyRepository()
        async with policy_db.read_connection() as conn:
            policy = await repo.get_policy(conn, "p1")
        assert policy is not None
        assert policy["rules"] == []

    @pytest.mark.asyncio
    async def test_sqlite_store_round_trip_with_service_semantics(
        self, policy_db: Database
    ) -> None:
        """SQLite 仓库持久化策略后，语义与内存 Service 一致。

        由于 ``CapabilityPolicyStore`` Protocol 是同步接口而 SQLite 仓库是
        异步的，本测试分开验证：
        1. 通过 SQLite 仓库写入策略；
        2. 读取后构造等价的内存 store + Service，验证评估语义一致。
        生产代码集成 SQLite store 时应通过同步适配器包装异步仓库。
        """
        repo = SqliteCapabilityPolicyRepository()
        rule = _make_rule(roles=["DESIGNER"], cap_name="tool.x")

        async with policy_db.write_connection() as conn:
            await repo.upsert_policy(
                conn, "p1", "n1", [rule], 10, True, created_by="admin-1"
            )

        async with policy_db.read_connection() as conn:
            fetched = await repo.get_policy(conn, "p1")

        assert fetched is not None
        # 用读取出的策略构造内存 store + Service，验证语义
        store = InMemoryCapabilityPolicyStore()
        store.upsert_policy(
            fetched["policy_id"],
            fetched["name"],
            fetched["rules"],
            fetched["priority"],
            fetched["enabled"],
            created_by="admin-1",
        )
        svc = CapabilityPolicyServiceImpl(store=store, seed_defaults=False)

        req = _make_request(
            actor_roles=["DESIGNER"],
            capability_type="tool",
            capability_name="tool.x",
        )
        decision = await svc.evaluate(req)
        assert decision["allowed"] is True


# --------------------------------------------------------------------------- #
# 与 CasbinPermissionService 的正交性测试
# --------------------------------------------------------------------------- #


class TestCasbinOrthogonality:
    """CapabilityPolicy 与 CasbinPermissionService 正交。

    CapabilityPolicy 不调用 Casbin，两者可独立使用。
    """

    @pytest.mark.asyncio
    async def test_capability_policy_does_not_call_casbin(self) -> None:
        """CapabilityPolicy 评估不依赖 Casbin。"""
        from maf_policy import CasbinPermissionService

        casbin_svc = CasbinPermissionService()
        # Casbin 中 OBSERVER 没有 tools:write
        assert casbin_svc.check_permission("OBSERVER", "tools", "write") is False

        # 但 CapabilityPolicy 可以独立允许 OBSERVER 使用某 Tool
        svc = build_empty_capability_policy_service()
        rule = _make_rule(roles=["OBSERVER"], cap_name="tool.x", allowed=True)
        await svc.set_policy("obs-tool", [rule], actor_id="admin-1")
        req = _make_request(
            actor_roles=["OBSERVER"],
            capability_type="tool",
            capability_name="tool.x",
        )
        decision = await svc.evaluate(req)
        assert decision["allowed"] is True
        # Casbin 的判定不变
        assert casbin_svc.check_permission("OBSERVER", "tools", "write") is False

    @pytest.mark.asyncio
    async def test_two_layers_can_compose(self) -> None:
        """两层可组合：先 Casbin 检查资源访问，再 Capability 检查能力使用。"""
        from maf_contracts.common import ActorContext
        from maf_policy import CasbinPermissionService

        casbin_svc = CasbinPermissionService()
        cap_svc = build_empty_capability_policy_service()
        # 给 DESIGNER 允许 tool.filesystem.read 的能力策略
        rule = _make_rule(
            roles=["DESIGNER"],
            cap_type=CAPABILITY_TYPE_TOOL,
            cap_name="filesystem.read",
        )
        await cap_svc.set_policy("d-fs", [rule], actor_id="admin-1")

        actor: ActorContext = ActorContext(
            user_id="u1",
            organization_id="org-1",
            permission_keys=["DESIGNER"],
            trace_id="t1",
        )

        # 第一层：Casbin 检查 tools:read（DESIGNER 通过）
        await casbin_svc.require(actor, "read", "tools")

        # 第二层：Capability 检查能否使用 filesystem.read
        req = _make_request(
            actor_id="u1",
            actor_roles=["DESIGNER"],
            capability_type="tool",
            capability_name="filesystem.read",
        )
        decision = await cap_svc.evaluate(req)
        assert decision["allowed"] is True


# --------------------------------------------------------------------------- #
# fail-closed 测试
# --------------------------------------------------------------------------- #


class TestFailClosed:
    """任何异常路径默认拒绝。"""

    @pytest.mark.asyncio
    async def test_store_exception_returns_policy_error(self) -> None:
        """存储读取异常返回 POLICY_ERROR 拒绝。"""

        class _BrokenStore:
            def list_policies(self):
                raise RuntimeError("database down")

            def get_policy(self, policy_id):
                raise RuntimeError("database down")

            def upsert_policy(self, *args, **kwargs):
                raise RuntimeError("database down")

            def delete_policy(self, policy_id):
                raise RuntimeError("database down")

        svc = CapabilityPolicyServiceImpl(store=_BrokenStore(), seed_defaults=False)  # type: ignore[arg-type]
        req = _make_request(actor_roles=["OBSERVER"], capability_name="x")
        decision = await svc.evaluate(req)
        assert decision["allowed"] is False
        assert decision["reason"] == REASON_POLICY_ERROR

    @pytest.mark.asyncio
    async def test_rule_exception_skipped(self) -> None:
        """单条规则匹配异常时跳过，继续评估下一条。"""

        class _BadRule(dict):
            """模拟在评估时抛异常的规则。

            ``rule_matches`` 会调用 ``rule.get("match")``，本类在该调用上
            抛 ``ValueError``，触发 Service 的 try/except 跳过逻辑。
            """

            def get(self, key, default=None):
                if key == "match":
                    raise ValueError("boom")
                return super().get(key, default)

        good_rule = _make_rule(roles=["OWNER"], cap_name="tool.x", allowed=True)
        bad_rule = _BadRule()  # type: ignore[empty-body]

        class _BadRuleStore:
            """自定义 store：list_policies 直接返回含异常规则的策略列表。

            避免经过 ``InMemoryCapabilityPolicyStore._copy_rule`` 拷贝时
            触发异常（拷贝也会调用 ``rule.get("match")``）。
            """

            def list_policies(self):
                return [
                    CapabilityPolicy(
                        policy_id="p1",
                        name="n1",
                        rules=[bad_rule, good_rule],  # type: ignore[list-item]
                        priority=10,
                        enabled=True,
                    )
                ]

            def get_policy(self, policy_id):
                return None

            def upsert_policy(
                self, *args, **kwargs
            ):
                raise RuntimeError("not supported")

            def delete_policy(self, policy_id):
                return False

        svc = CapabilityPolicyServiceImpl(
            store=_BadRuleStore(),  # type: ignore[arg-type]
            seed_defaults=False,
        )
        req = _make_request(
            actor_roles=["OWNER"],
            capability_type="tool",
            capability_name="tool.x",
        )
        decision = await svc.evaluate(req)
        # 异常规则被跳过，正常规则匹配 → 允许
        assert decision["allowed"] is True

    @pytest.mark.asyncio
    async def test_batch_exception_returns_policy_error(self) -> None:
        """批量评估中单条异常不影响其他。"""
        svc = build_default_capability_policy_service()
        requests = [
            _make_request(actor_roles=["ADMIN"], capability_name="a"),
            "not-a-dict",  # type: ignore[list-item]
            _make_request(actor_roles=["OBSERVER"], capability_name="b"),
        ]
        decisions = await svc.evaluate_batch(requests)  # type: ignore[arg-type]
        assert len(decisions) == 3
        assert decisions[0]["allowed"] is True
        assert decisions[1]["allowed"] is False
        assert decisions[1]["reason"] == REASON_INVALID_REQUEST
        assert decisions[2]["allowed"] is False


# --------------------------------------------------------------------------- #
# 多角色组合测试
# --------------------------------------------------------------------------- #


class TestMultiRoleActor:
    """多角色 actor 评估。"""

    @pytest.mark.asyncio
    async def test_admin_role_dominates(self) -> None:
        """含 ADMIN 角色的多角色 actor 走 ADMIN 短路。"""
        svc = build_empty_capability_policy_service()
        # 即使有 OBSERVER 角色和拒绝策略，ADMIN 短路优先
        await svc.set_policy(
            "deny-observer",
            [_make_rule(roles=["OBSERVER"], cap_name="*", allowed=False, reason="OBSERVER_DENIED")],
            actor_id="admin-1",
            priority=1000,
        )
        req = _make_request(
            actor_roles=["OBSERVER", "ADMIN"],
            capability_type="tool",
            capability_name="any.thing",
        )
        decision = await svc.evaluate(req)
        assert decision["allowed"] is True
        assert decision["reason"] == REASON_ADMIN_DEFAULT_ALLOW

    @pytest.mark.asyncio
    async def test_multi_role_any_role_matches(self) -> None:
        """多角色 actor 中任一角色命中规则即生效。"""
        svc = build_empty_capability_policy_service()
        # 给 OWNER 允许 tool.x
        await svc.set_policy(
            "owner-allow",
            [_make_rule(roles=["OWNER"], cap_name="tool.x", allowed=True)],
            actor_id="admin-1",
        )
        req = _make_request(
            actor_roles=["OBSERVER", "OWNER"],
            capability_type="tool",
            capability_name="tool.x",
        )
        decision = await svc.evaluate(req)
        assert decision["allowed"] is True


# --------------------------------------------------------------------------- #
# 环境清理
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_maf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除 MAF_* 环境变量。"""
    for key in list(os.environ):
        if key.startswith("MAF_"):
            monkeypatch.delenv(key, raising=False)
