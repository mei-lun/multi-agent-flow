"""Quality Gate 定义校验辅助（TASK-081 增量）。

本模块为 Review/QualityGate 服务提供确定性的 GateDefinition 校验原语，
与 ``packages/artifact_schemas`` 既有的 Schema/lineage 校验辅助并列。

设计依据：doc/开发任务/06-交付评审/TASK-081-Review与QualityGate.md
- GateDefinition：name/validator/required_status/blocking 四元组；
- ``required_status`` 取值与 ``ValidatorStatus``（PASS/FAIL/ERROR）对齐，
  ERROR 必须视为失败，不能降级为 PASS（TASK-080 验收标准 1）；
- 校验是纯函数，无 LLM、无随机、无时间相关判断，相同输入永远返回相同结果。
"""

from __future__ import annotations

import re
from typing import Any

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: Validator 校验状态字面量集合（与 artifacts.service.ValidatorStatus 对齐）。
#: - ``PASS``：所有校验通过；
#: - ``FAIL``：校验发现阻断问题；
#: - ``ERROR``：Validator 自身出错，必须视为失败，不能降级为 PASS。
KNOWN_VALIDATOR_STATUSES: frozenset[str] = frozenset({"PASS", "FAIL", "ERROR"})

#: 评审工作流状态字面量集合（ReviewStatus）。
#: - ``PENDING``：已提交评审，等待人工决策；
#: - ``APPROVED``：人工批准；
#: - ``REJECTED``：人工拒绝；
#: - ``CHANGES_REQUESTED``：请求修改后重新提交。
KNOWN_REVIEW_STATUSES: frozenset[str] = frozenset(
    {"PENDING", "APPROVED", "REJECTED", "CHANGES_REQUESTED"}
)

#: Gate 名称合法字符（小写字母/数字/下划线/连字符），与 schema_name 规则对齐。
_GATE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

#: Validator 名称合法字符（允许冒号分隔，如 ``json_schema:task_payload:v1``）。
_VALIDATOR_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_:\-]*$")


def validate_gate_name(name: str) -> str:
    """校验 Gate 名称格式：小写字母开头，仅含小写字母/数字/下划线/连字符，1~64 字符。

    :raises ValueError: 名称非法。
    """
    if not isinstance(name, str) or not name:
        raise ValueError("gate name 不能为空")
    if len(name) > 64:
        raise ValueError(f"gate name 过长（>64）: {name!r}")
    if not _GATE_NAME_RE.match(name):
        raise ValueError(
            f"gate name 必须以小写字母开头，仅含小写字母/数字/下划线/连字符: {name!r}"
        )
    return name


def validate_validator_name(validator: str) -> str:
    """校验 Validator 名称格式：字母开头，允许字母/数字/下划线/冒号/连字符，1~128 字符。

    :raises ValueError: 名称非法。
    """
    if not isinstance(validator, str) or not validator:
        raise ValueError("validator 不能为空")
    if len(validator) > 128:
        raise ValueError(f"validator 名称过长（>128）: {validator!r}")
    if not _VALIDATOR_NAME_RE.match(validator):
        raise ValueError(
            f"validator 名称必须以字母开头，仅含字母/数字/下划线/冒号/连字符: "
            f"{validator!r}"
        )
    return validator


def validate_required_status(required_status: str) -> str:
    """校验 required_status 取值在 ``KNOWN_VALIDATOR_STATUSES`` 内。

    :raises ValueError: 取值非法。
    """
    if required_status not in KNOWN_VALIDATOR_STATUSES:
        raise ValueError(
            f"未知 required_status: {required_status!r}，合法值: "
            f"{sorted(KNOWN_VALIDATOR_STATUSES)}"
        )
    return required_status


def validate_gate_definition(definition: dict[str, Any]) -> dict[str, Any]:
    """校验单个 GateDefinition dict 的字段完整性与取值合法性。

    要求字段：
        - ``name``：str，合法 gate name；
        - ``validator``：str，合法 validator name；
        - ``required_status``：PASS/FAIL/ERROR；
        - ``blocking``：bool。

    :raises ValueError: 任一字段缺失或取值非法。
    """
    if not isinstance(definition, dict):
        raise ValueError(f"gate definition 必须是 dict: {type(definition).__name__}")

    name = definition.get("name")
    validate_gate_name(name if isinstance(name, str) else "")

    validator = definition.get("validator")
    validate_validator_name(validator if isinstance(validator, str) else "")

    required_status = definition.get("required_status")
    validate_required_status(required_status if isinstance(required_status, str) else "")

    blocking = definition.get("blocking")
    if not isinstance(blocking, bool):
        raise ValueError(
            f"blocking 必须是 bool: {blocking!r} (type={type(blocking).__name__})"
        )

    return definition


def validate_gate_definitions(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """校验 GateDefinition 列表：非空、每项合法、name 不重复。

    :raises ValueError: 列表为空、任一项非法、或存在重复 name。
    """
    if not isinstance(definitions, list):
        raise ValueError("gate_definitions 必须是 list")
    if not definitions:
        raise ValueError("gate_definitions 不能为空")

    seen_names: set[str] = set()
    for item in definitions:
        validate_gate_definition(item)
        item_name = item["name"]
        if item_name in seen_names:
            raise ValueError(f"gate name 重复: {item_name!r}")
        seen_names.add(item_name)

    return definitions


__all__ = [
    "KNOWN_REVIEW_STATUSES",
    "KNOWN_VALIDATOR_STATUSES",
    "validate_gate_definition",
    "validate_gate_definitions",
    "validate_gate_name",
    "validate_required_status",
    "validate_validator_name",
]
