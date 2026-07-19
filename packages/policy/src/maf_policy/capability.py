"""Capability policy data structures, default policies and storage.

TASK-050 范围：
- 定义 ``CapabilityRequest`` / ``CapabilityDecision`` / ``PolicyRule`` /
  ``CapabilityPolicy`` 等 TypedDict，作为 ``CapabilityPolicyService`` 的稳定契约。
- 提供 ``CapabilityPolicyStore`` Protocol 与两个实现：
    * ``InMemoryCapabilityPolicyStore``：进程内存储，供测试与服务默认使用；
    * ``SqliteCapabilityPolicyRepository``：``capability_policies`` 表 CRUD。
- 默认策略遵循 **deny-by-default** 原则：
    * 未匹配任何策略 → 拒绝；
    * ADMIN 角色由 Service 层短路放行（不依赖具体策略）；
    * 其他角色必须命中显式 ``allowed=True`` 的规则才放行。

与 ``CasbinPermissionService``（TASK-031）的关系：
- Casbin 管粗粒度资源访问（read/write/manage 资源类）；
- CapabilityPolicy 管细粒度能力使用（哪个 Tool/Model/Skill 可被哪个角色使用）；
- 两者正交：CapabilityPolicy 假定调用方已通过 Casbin 资源访问检查；
  在 ``CapabilityPolicyService.evaluate`` 内部不再调用 Casbin。

对应《多 Agent 协同工具系统设计文档》：
- §7.4 ``capability_policies`` 表；
- §10.5 ``CapabilityDecision``；
- §11.5 ``CapabilityPolicyService`` / ``PolicyEngine`` fail-closed；
- §15.5 PolicyEngine Fail-closed：策略缺失/异常默认拒绝。
"""

from __future__ import annotations

import fnmatch
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Protocol, TypedDict, cast


# --------------------------------------------------------------------------- #
# 枚举与常量
# --------------------------------------------------------------------------- #


#: 能力类型取值（与设计文档 §11.5 ``resource.type`` 对齐）。
CAPABILITY_TYPE_TOOL: str = "tool"
CAPABILITY_TYPE_MODEL: str = "model"
CAPABILITY_TYPE_SKILL: str = "skill"

ALL_CAPABILITY_TYPES: frozenset[str] = frozenset(
    {CAPABILITY_TYPE_TOOL, CAPABILITY_TYPE_MODEL, CAPABILITY_TYPE_SKILL}
)

#: reason_code 稳定字符串（与 ``ReasonCode.POLICY_*`` 对齐，但不强依赖枚举）。
REASON_ADMIN_DEFAULT_ALLOW: str = "ADMIN_DEFAULT_ALLOW"
REASON_POLICY_ALLOWED: str = "POLICY_ALLOWED"
REASON_POLICY_DENIED: str = "POLICY_DENIED"
REASON_DEFAULT_DENY: str = "DEFAULT_DENY"
REASON_INVALID_REQUEST: str = "INVALID_REQUEST"
REASON_POLICY_ERROR: str = "POLICY_ERROR"


# --------------------------------------------------------------------------- #
# TypedDict 契约
# --------------------------------------------------------------------------- #


class CapabilityRequest(TypedDict):
    """能力使用评估请求。

    - ``actor_id``：调用者用户/节点 ID；
    - ``actor_roles``：调用者角色列表（与 ``ActorContext.permission_keys`` 同源，
      可包含 ADMIN/DESIGNER/OWNER/APPROVER/OBSERVER 等已知角色）；
    - ``capability_type``：``"tool"`` / ``"model"`` / ``"skill"``；
    - ``capability_name``：能力名（Tool name / Model connection name / Skill name）；
    - ``capability_version``：能力版本，可空表示「任意版本」；
    - ``context``：附加运行时上下文（``project_id``、``run_id``、``assignment_epoch``
      等），供未来 Validator 扩展使用，当前不参与匹配。
    """

    actor_id: str
    actor_roles: list[str]
    capability_type: str
    capability_name: str
    capability_version: str | None
    context: dict[str, Any]


class CapabilityDecision(TypedDict):
    """能力使用评估决策。

    - ``allowed``：是否允许使用该能力；
    - ``reason``：稳定 reason_code 字符串（取值见 ``REASON_*`` 常量），
      便于审计与节点按确定原因重试；
    - ``conditions``：附加约束（如 ``"rate_limit:100/hour"``、
      ``"memory_limit:512m"``），允许时由规则附加；拒绝时通常为空列表。
    """

    allowed: bool
    reason: str
    conditions: list[str]


class CapabilityMatch(TypedDict):
    """规则匹配条件。

    所有字段均为「可选匹配」语义：

    - ``actor_roles``：``None`` 或空列表表示匹配任意角色；非空时
      ``request.actor_roles`` 与本列表存在**交集**即视为角色匹配；
    - ``capability_type``：``None`` 表示匹配任意类型；非空时必须相等；
    - ``capability_name``：fnmatch 模式（支持 ``*`` / ``?``），
      空字符串等价于 ``"*"``；
    - ``capability_version``：``None`` 表示匹配任意版本；非空时必须相等。
    """

    actor_roles: list[str] | None
    capability_type: str | None
    capability_name: str
    capability_version: str | None


class PolicyRule(TypedDict):
    """单条能力策略规则：匹配条件 + 决策。"""

    match: CapabilityMatch
    decision: CapabilityDecision


class CapabilityPolicy(TypedDict):
    """能力策略集合。

    - ``policy_id``：策略稳定 ID；
    - ``name``：人类可读名称；
    - ``rules``：规则列表，按数组顺序匹配，首条命中即生效；
    - ``priority``：优先级，**数值越大优先级越高**，先于低优先级策略评估；
    - ``enabled``：是否启用，禁用的策略不参与评估。
    """

    policy_id: str
    name: str
    rules: list[PolicyRule]
    priority: int
    enabled: bool


class CapabilityPolicyRecord(TypedDict):
    """``capability_policies`` 表完整记录（含审计字段）。"""

    policy_id: str
    name: str
    rules: list[PolicyRule]
    priority: int
    enabled: bool
    created_by: str
    created_at: str
    version_no: int


# --------------------------------------------------------------------------- #
# 默认决策构造工具
# --------------------------------------------------------------------------- #


def allow_decision(
    reason: str = REASON_POLICY_ALLOWED,
    conditions: list[str] | None = None,
) -> CapabilityDecision:
    """构造允许决策。"""
    return CapabilityDecision(
        allowed=True,
        reason=reason,
        conditions=list(conditions) if conditions else [],
    )


def deny_decision(reason: str = REASON_DEFAULT_DENY) -> CapabilityDecision:
    """构造拒绝决策（不带 conditions）。"""
    return CapabilityDecision(allowed=False, reason=reason, conditions=[])


# --------------------------------------------------------------------------- #
# 匹配算法
# --------------------------------------------------------------------------- #


def _role_matches(match_roles: list[str] | None, request_roles: list[str]) -> bool:
    """角色匹配：``None``/空列表通配，否则要求交集非空。"""
    if not match_roles:
        return True
    if not request_roles:
        return False
    match_set = {r for r in match_roles if isinstance(r, str) and r}
    request_set = {r for r in request_roles if isinstance(r, str) and r}
    return bool(match_set & request_set)


def _type_matches(
    match_type: str | None, request_type: str
) -> bool:
    """能力类型匹配：``None`` 通配，否则字符串相等。"""
    if not match_type:
        return True
    return match_type == request_type


def _name_matches(match_name: str, request_name: str) -> bool:
    """能力名匹配：fnmatch 模式，空模式等价于 ``"*"``。"""
    pattern = match_name if match_name else "*"
    return fnmatch.fnmatchcase(request_name, pattern)


def _version_matches(
    match_version: str | None, request_version: str | None
) -> bool:
    """版本匹配：``None`` 通配任意版本；request 未指定版本时仅匹配通配规则。"""
    if match_version is None:
        return True
    if request_version is None:
        # 请求未指定版本时，规则指定具体版本则不匹配
        return False
    return fnmatch.fnmatchcase(request_version, match_version)


def rule_matches(rule: PolicyRule, request: CapabilityRequest) -> bool:
    """判断单条规则是否匹配请求。"""
    match = rule.get("match") or {}
    if not _role_matches(match.get("actor_roles"), request.get("actor_roles") or []):
        return False
    if not _type_matches(match.get("capability_type"), request.get("capability_type") or ""):
        return False
    if not _name_matches(match.get("capability_name") or "*", request.get("capability_name") or ""):
        return False
    if not _version_matches(match.get("capability_version"), request.get("capability_version")):
        return False
    return True


# --------------------------------------------------------------------------- #
# 存储层 Protocol
# --------------------------------------------------------------------------- #


class CapabilityPolicyStore(Protocol):
    """能力策略存储协议。"""

    def list_policies(self) -> list[CapabilityPolicy]:
        """返回全部策略（含禁用）。"""
        ...

    def get_policy(self, policy_id: str) -> CapabilityPolicy | None:
        """按 ID 获取策略；不存在返回 ``None``。"""
        ...

    def upsert_policy(
        self,
        policy_id: str,
        name: str,
        rules: list[PolicyRule],
        priority: int,
        enabled: bool,
        *,
        created_by: str,
    ) -> CapabilityPolicy:
        """新增或更新策略；返回写入后的策略视图。"""
        ...

    def delete_policy(self, policy_id: str) -> bool:
        """删除策略；存在并删除返回 ``True``。"""
        ...


# --------------------------------------------------------------------------- #
# 内存存储
# --------------------------------------------------------------------------- #


class InMemoryCapabilityPolicyStore:
    """进程内策略存储，供测试与服务默认依赖。

    线程安全语义：本类不做锁，假定调用方在 asyncio 单线程事件循环中使用；
    若跨线程使用，调用方自行加锁。
    """

    def __init__(self) -> None:
        self._policies: dict[str, CapabilityPolicy] = {}

    def list_policies(self) -> list[CapabilityPolicy]:
        return [self._copy_policy(p) for p in self._policies.values()]

    def get_policy(self, policy_id: str) -> CapabilityPolicy | None:
        p = self._policies.get(policy_id)
        return self._copy_policy(p) if p is not None else None

    def upsert_policy(
        self,
        policy_id: str,
        name: str,
        rules: list[PolicyRule],
        priority: int,
        enabled: bool,
        *,
        created_by: str,  # noqa: ARG002 —— 内存存储忽略 created_by，保留参数以对齐 Protocol
    ) -> CapabilityPolicy:
        policy = CapabilityPolicy(
            policy_id=policy_id,
            name=name,
            rules=[self._copy_rule(r) for r in rules],
            priority=int(priority),
            enabled=bool(enabled),
        )
        self._policies[policy_id] = policy
        return self._copy_policy(policy)

    def delete_policy(self, policy_id: str) -> bool:
        return self._policies.pop(policy_id, None) is not None

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _copy_rule(rule: PolicyRule) -> PolicyRule:
        return PolicyRule(
            match=cast(CapabilityMatch, dict(rule.get("match") or {})),
            decision=cast(CapabilityDecision, dict(rule.get("decision") or {})),
        )

    @classmethod
    def _copy_policy(cls, policy: CapabilityPolicy) -> CapabilityPolicy:
        return CapabilityPolicy(
            policy_id=policy["policy_id"],
            name=policy["name"],
            rules=[cls._copy_rule(r) for r in policy.get("rules") or []],
            priority=policy.get("priority", 0),
            enabled=bool(policy.get("enabled", True)),
        )


# --------------------------------------------------------------------------- #
# SQLite 存储与 DDL
# --------------------------------------------------------------------------- #


#: ``capability_policies`` 表 DDL（与设计文档 §7.4 对齐）。
#:
#: 字段说明：
#: - ``policy_id`` TEXT PK：策略稳定 ID；
#: - ``name`` TEXT：人类可读名称；
#: - ``rules`` TEXT NOT NULL：``list[PolicyRule]`` 的 JSON 序列化；
#: - ``priority`` INTEGER NOT NULL DEFAULT 0：优先级，数值越大越先匹配；
#: - ``enabled`` INTEGER NOT NULL DEFAULT 1：0/1 表示禁用/启用；
#: - ``created_by`` TEXT NOT NULL：创建者 user_id；
#: - ``created_at`` TEXT NOT NULL：RFC 3339 时间戳；
#: - ``version_no`` INTEGER NOT NULL DEFAULT 1：每次 upsert 自增，用于乐观锁。
CAPABILITY_POLICIES_SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS capability_policies (
    policy_id   TEXT PRIMARY KEY NOT NULL,
    name        TEXT NOT NULL,
    rules       TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    version_no  INTEGER NOT NULL DEFAULT 1
);
"""


def _now_iso() -> str:
    """UTC RFC 3339 时间戳。"""
    return datetime.now(timezone.utc).isoformat()


def _rules_to_json(rules: list[PolicyRule]) -> str:
    """序列化规则列表为 JSON 字符串。"""
    return json.dumps(list(rules), ensure_ascii=False, sort_keys=True)


def _rules_from_json(text: str) -> list[PolicyRule]:
    """反序列化规则列表；异常时返回空列表（fail-closed）。"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [cast(PolicyRule, r) for r in data if isinstance(r, dict)]


def _row_to_policy(row: sqlite3.Row | tuple) -> CapabilityPolicy:
    """将数据库行转换为 ``CapabilityPolicy`` 视图（不含审计字段）。"""
    if isinstance(row, sqlite3.Row):
        policy_id = row["policy_id"]
        name = row["name"]
        rules_text = row["rules"]
        priority = row["priority"]
        enabled_int = row["enabled"]
    else:
        policy_id, name, rules_text, priority, enabled_int = row[:5]
    return CapabilityPolicy(
        policy_id=policy_id,
        name=name,
        rules=_rules_from_json(rules_text),
        priority=int(priority),
        enabled=bool(enabled_int),
    )


def _row_to_record(row: sqlite3.Row | tuple) -> CapabilityPolicyRecord:
    """将数据库行转换为 ``CapabilityPolicyRecord``（含审计字段）。"""
    if isinstance(row, sqlite3.Row):
        policy_id = row["policy_id"]
        name = row["name"]
        rules_text = row["rules"]
        priority = row["priority"]
        enabled_int = row["enabled"]
        created_by = row["created_by"]
        created_at = row["created_at"]
        version_no = row["version_no"]
    else:
        (
            policy_id,
            name,
            rules_text,
            priority,
            enabled_int,
            created_by,
            created_at,
            version_no,
        ) = row
    return CapabilityPolicyRecord(
        policy_id=policy_id,
        name=name,
        rules=_rules_from_json(rules_text),
        priority=int(priority),
        enabled=bool(enabled_int),
        created_by=created_by,
        created_at=created_at,
        version_no=int(version_no),
    )


class SqliteCapabilityPolicyRepository:
    """``capability_policies`` 表的 SQLite CRUD 实现。

    使用方式：
        - ``init_schema(conn)`` 在 ``Database.write_connection`` 内创建表；
        - ``list_policies(conn)`` / ``get_policy(conn, policy_id)`` 在
          ``Database.read_connection`` 内读取；
        - ``upsert_policy(conn, ...)`` / ``delete_policy(conn, ...)`` 在
          ``Database.write_connection`` 内写入。

    本仓库不维护事务边界，调用方（通常是 ``SqliteUnitOfWork``）负责提交/回滚。
    """

    def __init__(self) -> None:
        pass

    # ------------------------------------------------------------------ #
    # DDL
    # ------------------------------------------------------------------ #

    @staticmethod
    def init_schema(conn: sqlite3.Connection | Any) -> None:
        """在给定连接上创建 ``capability_policies`` 表（IF NOT EXISTS）。"""
        conn.execute(CAPABILITY_POLICIES_SCHEMA_SQL)

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #

    async def list_policies(self, conn: Any) -> list[CapabilityPolicy]:
        cur = await conn.execute(
            "SELECT policy_id, name, rules, priority, enabled "
            "FROM capability_policies ORDER BY priority DESC, policy_id ASC"
        )
        rows = await cur.fetchall()
        await cur.close()
        return [_row_to_policy(r) for r in rows]

    async def get_policy(
        self, conn: Any, policy_id: str
    ) -> CapabilityPolicy | None:
        cur = await conn.execute(
            "SELECT policy_id, name, rules, priority, enabled "
            "FROM capability_policies WHERE policy_id = ?",
            (policy_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return _row_to_policy(row) if row is not None else None

    async def get_record(
        self, conn: Any, policy_id: str
    ) -> CapabilityPolicyRecord | None:
        cur = await conn.execute(
            "SELECT policy_id, name, rules, priority, enabled, "
            "created_by, created_at, version_no "
            "FROM capability_policies WHERE policy_id = ?",
            (policy_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        return _row_to_record(row) if row is not None else None

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #

    async def upsert_policy(
        self,
        conn: Any,
        policy_id: str,
        name: str,
        rules: list[PolicyRule],
        priority: int,
        enabled: bool,
        *,
        created_by: str,
        now: str | None = None,
    ) -> CapabilityPolicyRecord:
        """插入或更新策略；存在时 ``version_no`` 自增。"""
        created_at = now or _now_iso()
        rules_text = _rules_to_json(rules)
        enabled_int = 1 if enabled else 0

        cur = await conn.execute(
            "SELECT version_no, created_by, created_at FROM capability_policies "
            "WHERE policy_id = ?",
            (policy_id,),
        )
        existing = await cur.fetchone()
        await cur.close()

        if existing is None:
            await conn.execute(
                "INSERT INTO capability_policies "
                "(policy_id, name, rules, priority, enabled, created_by, "
                " created_at, version_no) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    policy_id,
                    name,
                    rules_text,
                    int(priority),
                    enabled_int,
                    created_by,
                    created_at,
                ),
            )
            return CapabilityPolicyRecord(
                policy_id=policy_id,
                name=name,
                rules=list(rules),
                priority=int(priority),
                enabled=bool(enabled),
                created_by=created_by,
                created_at=created_at,
                version_no=1,
            )

        # 更新现有记录
        old_version_no = int(existing[0])
        preserved_created_by = existing[1] or created_by
        preserved_created_at = existing[2] or created_at
        new_version_no = old_version_no + 1
        await conn.execute(
            "UPDATE capability_policies "
            "SET name = ?, rules = ?, priority = ?, enabled = ?, version_no = ? "
            "WHERE policy_id = ?",
            (
                name,
                rules_text,
                int(priority),
                enabled_int,
                new_version_no,
                policy_id,
            ),
        )
        return CapabilityPolicyRecord(
            policy_id=policy_id,
            name=name,
            rules=list(rules),
            priority=int(priority),
            enabled=bool(enabled),
            created_by=preserved_created_by,
            created_at=preserved_created_at,
            version_no=new_version_no,
        )

    async def delete_policy(self, conn: Any, policy_id: str) -> bool:
        cur = await conn.execute(
            "DELETE FROM capability_policies WHERE policy_id = ?",
            (policy_id,),
        )
        deleted = cur.rowcount or 0
        await cur.close()
        return deleted > 0

    async def set_enabled(
        self, conn: Any, policy_id: str, enabled: bool
    ) -> bool:
        """仅切换启用状态；返回是否命中。"""
        cur = await conn.execute(
            "UPDATE capability_policies SET enabled = ? WHERE policy_id = ?",
            (1 if enabled else 0, policy_id),
        )
        affected = cur.rowcount or 0
        await cur.close()
        return affected > 0


# --------------------------------------------------------------------------- #
# 默认内置策略
# --------------------------------------------------------------------------- #


#: 默认 ADMIN 全允许策略 ID。
DEFAULT_ADMIN_ALLOW_POLICY_ID: str = "default-admin-allow"

#: 默认 OBSERVER tool 只读策略 ID（示例，演示细粒度配置）。
DEFAULT_OBSERVER_TOOL_READ_POLICY_ID: str = "default-observer-tool-read"


def _build_default_admin_allow_policy() -> CapabilityPolicy:
    """构造默认 ADMIN 全允许策略。

    注意：本策略在 Service 层的 ADMIN 短路逻辑中作为兜底；
    保留此策略是为了让外部观察者能通过 ``list_policies`` 看到 ADMIN 的默认行为。
    """
    return CapabilityPolicy(
        policy_id=DEFAULT_ADMIN_ALLOW_POLICY_ID,
        name="Default ADMIN allow-all",
        rules=[
            PolicyRule(
                match=CapabilityMatch(
                    actor_roles=["ADMIN"],
                    capability_type=None,
                    capability_name="*",
                    capability_version=None,
                ),
                decision=allow_decision(reason=REASON_ADMIN_DEFAULT_ALLOW),
            )
        ],
        priority=1000,
        enabled=True,
    )


def _build_default_observer_tool_read_policy() -> CapabilityPolicy:
    """构造默认 OBSERVER tool 列表只读策略（演示细粒度能力）。"""
    return CapabilityPolicy(
        policy_id=DEFAULT_OBSERVER_TOOL_READ_POLICY_ID,
        name="Default OBSERVER tool list/read",
        rules=[
            PolicyRule(
                match=CapabilityMatch(
                    actor_roles=["OBSERVER"],
                    capability_type=CAPABILITY_TYPE_TOOL,
                    capability_name="*.list",
                    capability_version=None,
                ),
                decision=allow_decision(reason=REASON_POLICY_ALLOWED),
            )
        ],
        priority=100,
        enabled=True,
    )


def build_default_capability_policies() -> list[CapabilityPolicy]:
    """返回默认内置能力策略列表。

    默认策略仅包含 ADMIN 全允许，确保 ADMIN 在 store 层也能匹配；
    其他角色（DESIGNER/OWNER/APPROVER/OBSERVER）默认无任何能力，必须由
    管理员通过 ``set_policy`` 显式授予，体现 **deny-by-default** 原则。
    """
    return [
        _build_default_admin_allow_policy(),
        _build_default_observer_tool_read_policy(),
    ]


def seed_default_policies(store: CapabilityPolicyStore) -> None:
    """向存储中植入默认策略；已存在同 ID 的策略保留不动。"""
    for policy in build_default_capability_policies():
        if store.get_policy(policy["policy_id"]) is None:
            store.upsert_policy(
                policy_id=policy["policy_id"],
                name=policy["name"],
                rules=policy["rules"],
                priority=policy["priority"],
                enabled=policy["enabled"],
                created_by="system",
            )


__all__ = [
    # 常量
    "CAPABILITY_TYPE_TOOL",
    "CAPABILITY_TYPE_MODEL",
    "CAPABILITY_TYPE_SKILL",
    "ALL_CAPABILITY_TYPES",
    "REASON_ADMIN_DEFAULT_ALLOW",
    "REASON_POLICY_ALLOWED",
    "REASON_POLICY_DENIED",
    "REASON_DEFAULT_DENY",
    "REASON_INVALID_REQUEST",
    "REASON_POLICY_ERROR",
    "DEFAULT_ADMIN_ALLOW_POLICY_ID",
    "DEFAULT_OBSERVER_TOOL_READ_POLICY_ID",
    # TypedDict
    "CapabilityRequest",
    "CapabilityDecision",
    "CapabilityMatch",
    "PolicyRule",
    "CapabilityPolicy",
    "CapabilityPolicyRecord",
    # 工具
    "allow_decision",
    "deny_decision",
    "rule_matches",
    # 存储
    "CapabilityPolicyStore",
    "InMemoryCapabilityPolicyStore",
    "SqliteCapabilityPolicyRepository",
    "CAPABILITY_POLICIES_SCHEMA_SQL",
    # 默认策略
    "build_default_capability_policies",
    "seed_default_policies",
]
