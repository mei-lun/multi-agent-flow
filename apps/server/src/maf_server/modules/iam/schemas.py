"""IAM HTTP 与应用服务的输入输出字段。"""

from typing import Any, Literal, NotRequired, TypedDict


class LoginRequest(TypedDict):
    username: str
    password: str


class SessionView(TypedDict):
    session_id: str
    expires_at: str
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
    key: str
    value: Any
    value_type: str
    version: int
    updated_at: str


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

