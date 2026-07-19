"""Authorization policy loading and execution-boundary validators.

TASK-031 扩展：
- ``policy`` 模块提供基于 Casbin 的 ``CasbinPermissionService`` 实现，
  以及 5 个内置角色（ADMIN/DESIGNER/OWNER/APPROVER/OBSERVER）的默认策略。
- ``validators`` 模块保留供后续任务（Tool/Skill/Path/Network 边界校验器）使用。

TASK-050 扩展：
- ``capability`` 模块提供细粒度能力策略（CapabilityPolicy）数据结构、
  匹配算法、内存/SQLite 存储与默认策略，供
  ``maf_server.gateway.policy.capability_policy.CapabilityPolicyServiceImpl``
  使用。与 ``CasbinPermissionService`` 正交：Casbin 管资源访问，
  CapabilityPolicy 管 Tool/Model/Skill 使用许可。
"""

from .capability import (
    ALL_CAPABILITY_TYPES,
    CAPABILITY_POLICIES_SCHEMA_SQL,
    CAPABILITY_TYPE_MODEL,
    CAPABILITY_TYPE_SKILL,
    CAPABILITY_TYPE_TOOL,
    DEFAULT_ADMIN_ALLOW_POLICY_ID,
    DEFAULT_OBSERVER_TOOL_READ_POLICY_ID,
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
    CapabilityPolicyStore,
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
from .policy import (
    DEFAULT_POLICIES,
    KNOWN_ROLES,
    MODEL_CONF_PATH,
    CasbinPermissionService,
    validate_permission_keys,
)

__all__ = [
    # TASK-031
    "DEFAULT_POLICIES",
    "KNOWN_ROLES",
    "MODEL_CONF_PATH",
    "CasbinPermissionService",
    "validate_permission_keys",
    # TASK-050 capability
    "ALL_CAPABILITY_TYPES",
    "CAPABILITY_POLICIES_SCHEMA_SQL",
    "CAPABILITY_TYPE_MODEL",
    "CAPABILITY_TYPE_SKILL",
    "CAPABILITY_TYPE_TOOL",
    "DEFAULT_ADMIN_ALLOW_POLICY_ID",
    "DEFAULT_OBSERVER_TOOL_READ_POLICY_ID",
    "REASON_ADMIN_DEFAULT_ALLOW",
    "REASON_DEFAULT_DENY",
    "REASON_INVALID_REQUEST",
    "REASON_POLICY_ALLOWED",
    "REASON_POLICY_DENIED",
    "REASON_POLICY_ERROR",
    "CapabilityDecision",
    "CapabilityMatch",
    "CapabilityPolicy",
    "CapabilityPolicyRecord",
    "CapabilityPolicyStore",
    "CapabilityRequest",
    "InMemoryCapabilityPolicyStore",
    "PolicyRule",
    "SqliteCapabilityPolicyRepository",
    "allow_decision",
    "build_default_capability_policies",
    "deny_decision",
    "rule_matches",
    "seed_default_policies",
]
