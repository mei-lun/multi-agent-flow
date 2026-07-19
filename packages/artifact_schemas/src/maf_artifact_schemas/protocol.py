"""Protocol version, schema references, and validation result models.

与《GitHub 分布式协作协议》对齐：
- 协议版本 1 是当前唯一支持的版本；
- Schema 文件命名为 ``<type>-v<version>.schema.json``，如 ``task-v1``；
- ``project.yaml`` 通过 ``schema_version`` 字段声明所用协议版本。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ProtocolVersion(int, Enum):
    """MAF Git 协调协议版本。

    值与 ``project.yaml`` 的 ``schema_version`` 字段、各 Schema 文件的
    ``schema_version`` const 字段保持一致。新增版本只能追加，禁止重命名或复用。
    """

    V1 = 1

    @classmethod
    def latest(cls) -> ProtocolVersion:
        """返回当前最新协议版本。"""
        return cls.V1

    @classmethod
    def from_value(cls, value: int) -> ProtocolVersion:
        """从整数值解析协议版本，未知值抛 ``ValueError``。"""
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(f"未知的协议版本: {value}") from exc


# 已知的 Schema 名称（与 templates/git_coordination/schemas/ 下的文件对应）。
KnownSchemaName = Literal["project", "task", "node", "event"]


class SchemaRef(BaseModel):
    """对某个 Schema 的稳定引用，按名称 + 主版本定位。

    例如 ``SchemaRef(name="task", version=1)`` 对应 ``task-v1.schema.json``。
    """

    name: KnownSchemaName = Field(description="Schema 类型，如 task/node/event/project")
    version: int = Field(ge=1, description="Schema 主版本，从 1 开始")

    @property
    def file_stem(self) -> str:
        """对应的文件名（不含扩展名），如 ``task-v1``。"""
        return f"{self.name}-v{self.version}"

    @property
    def schema_id(self) -> str:
        """JSON Schema 的 ``$id`` 值，如 ``maf/task-v1``。"""
        return f"maf/{self.file_stem}"


class SchemaValidationIssue(BaseModel):
    """单条校验问题，携带文件路径与字段路径用于错误定位。"""

    file_path: str = Field(description="被校验文件的路径")
    field_path: str = Field(description="JSON Path 字段路径，如 $.task_id")
    message: str = Field(description="人类可读的校验失败说明")
    schema_id: str | None = Field(default=None, description="校验所用 Schema 的 $id")


class SchemaValidationResult(BaseModel):
    """Schema 校验结果。

    ``valid`` 为 True 时 ``issues`` 为空；为 False 时至少包含一条问题。
    """

    valid: bool = Field(description="是否通过校验")
    schema_ref: SchemaRef = Field(description="校验所用的 Schema 引用")
    issues: list[SchemaValidationIssue] = Field(
        default_factory=list, description="校验问题列表"
    )

    @classmethod
    def ok(cls, schema_ref: SchemaRef) -> SchemaValidationResult:
        return cls(valid=True, schema_ref=schema_ref, issues=[])

    @classmethod
    def fail(
        cls, schema_ref: SchemaRef, issues: list[SchemaValidationIssue]
    ) -> SchemaValidationResult:
        if not issues:
            raise ValueError("fail 结果必须至少包含一条 issue")
        return cls(valid=False, schema_ref=schema_ref, issues=issues)


# --------------------------------------------------------------------------- #
# Artifact Schema 注册与血缘关系类型（TASK-079 增量）
#
# 这些类型供 ArtifactSchemaService、lineage、diff 复用，与已有的
# SchemaRef/SchemaValidation* 共同构成 Artifact Schema 管理协议。
# 设计依据：doc/多Agent协同工具系统设计文档.md §7.7 artifact_lineage 与 §16.5 血缘。
# --------------------------------------------------------------------------- #


#: Artifact Schema 注册状态字面量。
#: - ``ACTIVE``：可用，可被 ``validate_artifact`` 引用；
#: - ``DEPRECATED``：已废弃，不允许新 artifact 引用，但历史引用仍可查询。
SchemaStatus = Literal["ACTIVE", "DEPRECATED"]


#: Artifact 血缘关系类型字面量（与设计文档 §16.5 对齐）。
#: - ``DERIVED_FROM``：通用派生（默认）；
#: - ``IMPLEMENTS``：Patch → Requirements/Blueprint；
#: - ``TESTS``：TestReport → Patch/Requirements；
#: - ``REVIEWS``：Review → Artifact/PR；
#: - ``SUPERSEDES``：返工新版本取代旧版本；
#: - ``DELIVERS``：DeliveryManifest → 最终工件。
LineageRelation = Literal[
    "DERIVED_FROM", "IMPLEMENTS", "TESTS", "REVIEWS", "SUPERSEDES", "DELIVERS"
]


#: 已知血缘关系集合，供 service 层校验。
KNOWN_LINEAGE_RELATIONS: frozenset[str] = frozenset(
    {
        "DERIVED_FROM",
        "IMPLEMENTS",
        "TESTS",
        "REVIEWS",
        "SUPERSEDES",
        "DELIVERS",
    }
)


def validate_schema_name(name: str) -> str:
    """校验 Schema 名称格式：小写字母/数字/下划线，1~64 字符。

    与 ``templates/git_coordination/schemas/<name>-v<version>.schema.json``
    命名规则保持一致，防止注入或路径越界。失败抛 ``ValueError``。
    """
    if not isinstance(name, str) or not name:
        raise ValueError("schema_name 不能为空")
    if len(name) > 64:
        raise ValueError(f"schema_name 过长（>64）: {name!r}")
    import re

    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        raise ValueError(
            f"schema_name 必须以小写字母开头，仅含小写字母/数字/下划线: {name!r}"
        )
    return name


def validate_schema_version(version: int) -> int:
    """校验 Schema 版本为 >= 1 的整数。失败抛 ``ValueError``。"""
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError(f"schema_version 必须为整数: {version!r}")
    if version < 1:
        raise ValueError(f"schema_version 必须 >= 1: {version}")
    return version


def validate_lineage_relation(relation: str) -> str:
    """校验血缘关系取值在 ``KNOWN_LINEAGE_RELATIONS`` 内。失败抛 ``ValueError``。"""
    if relation not in KNOWN_LINEAGE_RELATIONS:
        raise ValueError(
            f"未知 lineage relation: {relation!r}，合法值: "
            f"{sorted(KNOWN_LINEAGE_RELATIONS)}"
        )
    return relation
