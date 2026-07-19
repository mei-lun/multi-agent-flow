"""Versioned JSON Schema definitions and protocol version types for MAF.

本包存放可版本化的 Schema 类型与协议版本定义。物理 JSON Schema 文件位于
``templates/git_coordination/schemas/``，由 ``SchemaLoader`` 在运行时加载；
本包只提供协议版本枚举、Schema 引用模型和校验结果类型，不读取文件系统。

TASK-079 增量：新增 ``SchemaStatus``、``LineageRelation`` 及校验辅助函数，
供 ArtifactSchemaService 与 lineage 模块复用。

TASK-081 增量：新增 ``quality_gate`` 模块，提供 GateDefinition 校验辅助
（``validate_gate_definition`` / ``validate_gate_definitions`` 等），供
ReviewService 与 QualityGateService 复用。
"""

from maf_artifact_schemas.protocol import (
    KNOWN_LINEAGE_RELATIONS,
    KnownSchemaName,
    LineageRelation,
    ProtocolVersion,
    SchemaRef,
    SchemaStatus,
    SchemaValidationIssue,
    SchemaValidationResult,
    validate_lineage_relation,
    validate_schema_name,
    validate_schema_version,
)
from maf_artifact_schemas.quality_gate import (
    KNOWN_REVIEW_STATUSES,
    KNOWN_VALIDATOR_STATUSES,
    validate_gate_definition,
    validate_gate_definitions,
    validate_gate_name,
    validate_required_status,
    validate_validator_name,
)

__all__ = [
    "KNOWN_LINEAGE_RELATIONS",
    "KNOWN_REVIEW_STATUSES",
    "KNOWN_VALIDATOR_STATUSES",
    "KnownSchemaName",
    "LineageRelation",
    "ProtocolVersion",
    "SchemaRef",
    "SchemaStatus",
    "SchemaValidationIssue",
    "SchemaValidationResult",
    "validate_gate_definition",
    "validate_gate_definitions",
    "validate_gate_name",
    "validate_lineage_relation",
    "validate_required_status",
    "validate_schema_name",
    "validate_schema_version",
    "validate_validator_name",
]
