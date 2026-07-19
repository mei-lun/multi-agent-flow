"""IAM HTTP 与应用服务的输入输出字段。

TASK-030 扩展：
- ``SessionView`` 新增 ``token`` 字段，仅在 ``login`` 响应中携带明文 Session Token；
  ``get_current_user`` 等其他返回 ``SessionView`` 的接口应将其置空或省略。
- 新增 ``LoginResponse``：``login`` 响应的 DTO，与 ``SessionView`` 对齐，
  便于 FastAPI 直接序列化；保留 ``SessionView`` 兼容 ``IamService`` Protocol。

字段命名与 ``maf_contracts.common`` 保持一致；时间字段为带时区 ISO 8601 字符串。
"""

from typing import Any, Literal, NotRequired, TypedDict


class LoginRequest(TypedDict):
    username: str
    password: str


class SessionView(TypedDict):
    """会话视图。

    ``token`` 仅在 ``login`` 成功响应中返回一次明文；其他接口（如 ``get_current_user``）
    返回时不应包含 ``token``（使用 ``NotRequired`` 让序列化层可省略）。
    """

    session_id: str
    expires_at: str
    token: NotRequired[str]
    user: "UserView"


class LoginResponse(TypedDict):
    """``POST /api/v1/auth/login`` 成功响应。

    与 ``SessionView`` 字段对齐，便于 FastAPI 直接以 Pydantic 模型序列化输出。
    ``token`` 为明文 Session Token，仅在登录响应中返回一次，不写日志、不进审计。
    """

    session_id: str
    expires_at: str
    token: str
    user: "UserView"


class UserView(TypedDict):
    id: str
    username: str
    display_name: str
    status: Literal["ACTIVE", "DISABLED"]
    permissions: list[str]
    version: int


class CreateUserRequest(TypedDict):
    username: str
    display_name: str
    initial_password: str
    permission_keys: list[str]
    idempotency_key: str


class UpdateUserRequest(TypedDict, total=False):
    display_name: str
    status: Literal["ACTIVE", "DISABLED"]
    permission_keys: list[str]
    expected_version: int
    idempotency_key: str


class SettingView(TypedDict):
    """系统设置视图（TASK-032）。

    - ``value``：非敏感设置返回归一化后的值；敏感设置始终为 ``None``，
      调用方只能通过 ``configured`` 与 ``fingerprint`` 判断是否已配置。
    - ``is_secret``：是否为敏感设置（经 SecretService 存储）。
    - ``configured``：是否已配置过（数据库有对应行）。未配置时返回 Schema 默认值。
    - ``fingerprint``：敏感设置的不可逆指纹（``sha256(plaintext)[:8] + ".." + plaintext[-4:]``），
      非敏感设置为 ``None``。
    - ``updated_by``：最近一次更新的 actor_id（user_id）。
    """

    key: str
    value: Any
    value_type: str
    is_secret: bool
    configured: bool
    fingerprint: str | None
    version: int
    updated_at: str
    updated_by: str


class PutSettingRequest(TypedDict):
    value: Any
    expected_version: int | None
    idempotency_key: str


class UserQuery(TypedDict, total=False):
    cursor: str
    limit: int
    status: Literal["ACTIVE", "DISABLED"]
    keyword: str


class UserPage(TypedDict):
    items: list[UserView]
    next_cursor: str | None
    has_more: bool
