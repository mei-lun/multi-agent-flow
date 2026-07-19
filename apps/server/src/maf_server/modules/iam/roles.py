"""IAM 角色与权限资源常量。

TASK-031 范围：
- 定义 IAM 模块在 ``PermissionService.require`` 中使用的资源与动作常量，
  与 ``maf_policy.DEFAULT_POLICIES`` 中的资源/动作命名保持一致。
- 从 ``maf_policy`` 重导出 ``KNOWN_ROLES`` 与 ``validate_permission_keys``，
  便于 IAM service 层引用，避免直接依赖 ``maf_policy`` 内部路径。

对应设计文档 11.1 PermissionService：``require(actor, action, resource)`` 中
``action`` 和 ``resource`` 使用此处定义的稳定字符串。
"""

from __future__ import annotations

from maf_policy import KNOWN_ROLES, validate_permission_keys

# --------------------------------------------------------------------------- #
# 资源标识（与 DEFAULT_POLICIES 中的 obj 列对齐）
# --------------------------------------------------------------------------- #

RESOURCE_USERS: str = "users"
RESOURCE_PROJECTS: str = "projects"
RESOURCE_SKILLS: str = "skills"
RESOURCE_TOOLS: str = "tools"
RESOURCE_WORKFLOWS: str = "workflows"
RESOURCE_MODEL_CONNECTIONS: str = "model_connections"
RESOURCE_REVIEWS: str = "reviews"
RESOURCE_INBOX: str = "inbox"
RESOURCE_SETTINGS: str = "settings"
RESOURCE_REPOSITORIES: str = "repositories"

# --------------------------------------------------------------------------- #
# 动作标识（与 DEFAULT_POLICIES 中的 act 列对齐）
# --------------------------------------------------------------------------- #

ACTION_READ: str = "read"
ACTION_WRITE: str = "write"

__all__ = [
    "ACTION_READ",
    "ACTION_WRITE",
    "KNOWN_ROLES",
    "RESOURCE_INBOX",
    "RESOURCE_MODEL_CONNECTIONS",
    "RESOURCE_PROJECTS",
    "RESOURCE_REPOSITORIES",
    "RESOURCE_REVIEWS",
    "RESOURCE_SETTINGS",
    "RESOURCE_SKILLS",
    "RESOURCE_TOOLS",
    "RESOURCE_USERS",
    "RESOURCE_WORKFLOWS",
    "validate_permission_keys",
]
