"""Policy facade over PyCasbin and explicit execution validators.

TASK-050 扩展：
- ``capability_policy`` 模块提供 ``CapabilityPolicyServiceImpl`` 具体类，
  评估 actor 是否可使用指定 Tool/Model/Skill 能力。
- 保留 ``service`` 模块的 ``CapabilityPolicyService`` Protocol（旧契约，
  对应 ``evaluate(subject, action, resource, context)``）供 Tool Gateway
  后续迁移。
"""

from .capability_policy import (
    ADMIN_ROLE,
    CapabilityPolicyServiceImpl,
    build_default_capability_policy_service,
    build_empty_capability_policy_service,
)
from .service import CapabilityPolicyService

__all__ = [
    "ADMIN_ROLE",
    "CapabilityPolicyService",
    "CapabilityPolicyServiceImpl",
    "build_default_capability_policy_service",
    "build_empty_capability_policy_service",
]
