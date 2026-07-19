"""TASK-032：预定义系统设置 Schema 与值校验。

设计参考《多 Agent 协同工具系统设计文档》§25.1：

- 预定义 Schema 限定可设置的 key 集合，避免任意 key 写入造成配置面扩大；
- 敏感设置（``is_secret=True``，如 SMTP 密码、外部 API Key）只允许字符串值，
  其明文绝不进入 SQLite；非敏感设置按 ``value_type`` 归一化后以 JSON 持久化；
- ``validate_value`` 由 service 层在写入前调用，非法值抛 ``ArgumentError``。

本模块不连接数据库、不写日志、不依赖 FastAPI。
"""

from __future__ import annotations

from typing import Any

from maf_domain.errors import ArgumentError


class SettingSchema:
    """单个系统设置的元数据 Schema。

    - ``key``：稳定标识符（点分命名空间，如 ``system.name``、``smtp.password``）；
    - ``value_type``：``"string"``/``"integer"``/``"boolean"``/``"json"``；
    - ``is_secret``：True 表示经 ``SecretService`` 存储，SQLite 仅保存 ``secret_id``；
    - ``description``：人类可读说明；
    - ``default``：未配置时 ``get_setting`` 返回的默认值（可为 ``None``）。
    """

    __slots__ = ("key", "value_type", "is_secret", "description", "default")

    def __init__(
        self,
        *,
        key: str,
        value_type: str,
        is_secret: bool,
        description: str,
        default: Any = None,
    ) -> None:
        self.key = key
        self.value_type = value_type
        self.is_secret = is_secret
        self.description = description
        self.default = default


#: 预定义设置 Schema 表。新增 key 必须在此登记，未知 key 写入被 ``put_setting`` 拒绝。
SETTING_SCHEMAS: dict[str, SettingSchema] = {
    "system.name": SettingSchema(
        key="system.name",
        value_type="string",
        is_secret=False,
        description="系统显示名称",
        default="MAF",
    ),
    "system.timezone": SettingSchema(
        key="system.timezone",
        value_type="string",
        is_secret=False,
        description="系统默认时区（IANA 名称，如 UTC、Asia/Shanghai）",
        default="UTC",
    ),
    "smtp.host": SettingSchema(
        key="smtp.host",
        value_type="string",
        is_secret=False,
        description="SMTP 服务器主机名",
    ),
    "smtp.port": SettingSchema(
        key="smtp.port",
        value_type="integer",
        is_secret=False,
        description="SMTP 服务器端口",
    ),
    "smtp.username": SettingSchema(
        key="smtp.username",
        value_type="string",
        is_secret=False,
        description="SMTP 登录用户名",
    ),
    "smtp.password": SettingSchema(
        key="smtp.password",
        value_type="string",
        is_secret=True,
        description="SMTP 登录密码（经 SecretService 存储，不入 SQLite）",
    ),
    "external.github.api_key": SettingSchema(
        key="external.github.api_key",
        value_type="string",
        is_secret=True,
        description="GitHub API Token（经 SecretService 存储，不入 SQLite）",
    ),
}


def get_setting_schema(key: str) -> SettingSchema | None:
    """按 key 取预定义 Schema；未知 key 返回 ``None``。"""
    return SETTING_SCHEMAS.get(key)


def validate_value(schema: SettingSchema, value: Any) -> Any:
    """按 Schema 校验 value，返回归一化后的值；非法抛 ``ArgumentError``。

    - 敏感设置：必须是非空字符串（明文不在此处持久化，仅校验形态）；
    - ``string``：必须为 ``str``；
    - ``integer``：必须为 ``int`` 且非 ``bool``（Python 中 ``bool`` 是 ``int`` 子类）；
    - ``boolean``：必须为 ``bool``；
    - ``json``：必须为 ``dict`` 或 ``list``。
    """
    if schema.is_secret:
        if not isinstance(value, str) or not value:
            raise ArgumentError(
                f"敏感设置 {schema.key!r} 的值必须是非空字符串"
            )
        return value
    if schema.value_type == "string":
        if not isinstance(value, str):
            raise ArgumentError(
                f"设置 {schema.key!r} 期望 string，实际 {type(value).__name__}"
            )
        return value
    if schema.value_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ArgumentError(
                f"设置 {schema.key!r} 期望 integer，实际 {type(value).__name__}"
            )
        return value
    if schema.value_type == "boolean":
        if not isinstance(value, bool):
            raise ArgumentError(
                f"设置 {schema.key!r} 期望 boolean，实际 {type(value).__name__}"
            )
        return value
    if schema.value_type == "json":
        if not isinstance(value, (dict, list)):
            raise ArgumentError(
                f"设置 {schema.key!r} 期望 json（dict/list），实际 {type(value).__name__}"
            )
        return value
    raise ArgumentError(
        f"设置 {schema.key!r} 的 Schema 配置了未知 value_type: {schema.value_type!r}"
    )


__all__ = [
    "SettingSchema",
    "SETTING_SCHEMAS",
    "get_setting_schema",
    "validate_value",
]
