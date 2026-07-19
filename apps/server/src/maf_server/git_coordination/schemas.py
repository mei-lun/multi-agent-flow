"""SchemaLoader and YamlLoader for MAF Git coordination protocol.

加载 ``templates/git_coordination/schemas/`` 下的 JSON Schema，校验
``.maf/`` 目录中的 project.yaml、task、node、event 文件。校验失败时
抛出 ``ValidationError``，并在 ``context`` 中携带文件路径与字段路径，
对应《GitHub 分布式协作协议》第 3 节权威目录与第 5 节状态机。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from maf_artifact_schemas.protocol import (
    ProtocolVersion,
    SchemaRef,
    SchemaValidationIssue,
    SchemaValidationResult,
)
from maf_domain.errors import ArgumentError, ValidationError

# Schema 文件名模式：<name>-v<version>.schema.json
_SCHEMA_FILE_RE = re.compile(r"^(?P<name>[a-z]+)-v(?P<version>[0-9]+)\.schema\.json$")

# 项目根目录（apps/server/src/maf_server/git_coordination/schemas.py 向上 5 级）。
_PROJECT_ROOT = Path(__file__).resolve().parents[5]
_DEFAULT_SCHEMAS_DIR = _PROJECT_ROOT / "templates" / "git_coordination" / "schemas"

# jsonschema 对 required / additionalProperties 报错时 json_path 停在根 "$"，
# 需要从 message 中提取具体字段名，构造 $.<field> 路径用于错误定位。
_REQUIRED_FIELD_RE = re.compile(r"'([^']+)' is a required property")
_UNEXPECTED_FIELD_RE = re.compile(r"\('([^']+)' was unexpected\)")


def _resolve_field_path(error: JsonSchemaValidationError) -> str:
    """将 jsonschema 错误的 ``json_path`` 解析为带字段名的路径。

    - ``required`` 错误：json_path 是 "$"，从 message 提取缺失字段名；
    - ``additionalProperties`` 错误：json_path 是 "$"，从 message 提取额外字段名；
    - 其他错误（enum、type、const 等）：直接用 json_path。
    """
    if error.validator == "required" and error.json_path == "$":
        match = _REQUIRED_FIELD_RE.search(error.message)
        if match:
            return f"$.{match.group(1)}"
    elif error.validator == "additionalProperties" and error.json_path == "$":
        match = _UNEXPECTED_FIELD_RE.search(error.message)
        if match:
            return f"$.{match.group(1)}"
    return error.json_path


class YamlLoader:
    """加载 YAML 或 JSON 文件为字典。

    Git 协调协议的 ``project.yaml``、``tasks/*.yaml``、``nodes/*.yaml``
    使用 YAML 格式；``events/**/*.json`` 使用 JSON 格式。本类统一入口。
    """

    @staticmethod
    def load(path: Path) -> dict[str, Any]:
        """读取文件并解析为字典。

        - ``.json`` 后缀用 ``json.loads``；
        - ``.yaml`` / ``.yml`` 后缀用 ``yaml.safe_load``；
        - 其他后缀抛 ``ArgumentError``。
        """
        if not path.exists():
            raise ArgumentError(f"文件不存在: {path}")
        text = path.read_text(encoding="utf-8")
        suffix = path.suffix.lower()
        if suffix == ".json":
            data = json.loads(text)
        elif suffix in (".yaml", ".yml"):
            data = yaml.safe_load(text)
        else:
            raise ArgumentError(f"不支持的文件类型 '{suffix}': {path}")
        if not isinstance(data, dict):
            raise ArgumentError(
                f"文件内容不是对象/dict: {path} (实际类型: {type(data).__name__})"
            )
        return data


class SchemaLoader:
    """加载并缓存 ``templates/git_coordination/schemas/`` 下的 JSON Schema。

    使用方式::

        loader = SchemaLoader()
        loader.validate_file(Path(".maf/project.yaml"), SchemaRef(name="project", version=1))

    校验失败时抛 ``ValidationError``，``context`` 包含：
    - ``file_path``：被校验文件的路径；
    - ``field_path``：JSON Path 字段路径（如 ``$.task_id``）；
    - ``schema_id``：校验所用 Schema 的 ``$id``。
    """

    def __init__(self, schemas_dir: Path | None = None) -> None:
        self.schemas_dir: Path = schemas_dir if schemas_dir is not None else _DEFAULT_SCHEMAS_DIR
        self._schemas: dict[str, dict[str, Any]] = {}
        self._validators: dict[str, Draft202012Validator] = {}
        self._load_all()

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #

    def _load_all(self) -> None:
        if not self.schemas_dir.exists():
            raise ArgumentError(f"Schema 目录不存在: {self.schemas_dir}")
        for path in sorted(self.schemas_dir.glob("*.schema.json")):
            ref = self._parse_filename(path.name)
            if ref is None:
                continue
            if not self._is_known_version(ref.version):
                raise ArgumentError(
                    f"Schema 文件 '{path.name}' 声明了未知协议版本 v{ref.version}"
                )
            schema_doc = YamlLoader.load(path)
            self._validate_schema_document(schema_doc, path)
            key = ref.file_stem
            self._schemas[key] = schema_doc
            self._validators[key] = Draft202012Validator(schema_doc)

    @staticmethod
    def _parse_filename(filename: str) -> SchemaRef | None:
        match = _SCHEMA_FILE_RE.match(filename)
        if match is None:
            return None
        name = match.group("name")
        version = int(match.group("version"))
        try:
            return SchemaRef(name=name, version=version)  # type: ignore[arg-type]
        except Exception:
            return None

    @staticmethod
    def _is_known_version(version: int) -> bool:
        try:
            ProtocolVersion.from_value(version)
            return True
        except ValueError:
            return False

    @staticmethod
    def _validate_schema_document(schema_doc: dict[str, Any], path: Path) -> None:
        if "$id" not in schema_doc:
            raise ArgumentError(f"Schema 文件缺少 $id: {path}")
        if "type" not in schema_doc:
            raise ArgumentError(f"Schema 文件缺少 type 声明: {path}")

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def known_refs(self) -> list[SchemaRef]:
        """返回已加载的所有 Schema 引用。"""
        refs: list[SchemaRef] = []
        for key in sorted(self._schemas.keys()):
            name, version_str = key.rsplit("-v", 1)
            refs.append(SchemaRef(name=name, version=int(version_str)))  # type: ignore[arg-type]
        return refs

    def is_known_version(self, version: int) -> bool:
        """检查协议版本是否已知。"""
        return self._is_known_version(version)

    def get_schema(self, ref: SchemaRef) -> dict[str, Any]:
        """返回指定 Schema 的原始字典。未加载时抛 ``ArgumentError``。"""
        key = ref.file_stem
        if key not in self._schemas:
            raise ArgumentError(
                f"未加载的 Schema '{key}'，已知: {sorted(self._schemas.keys())}"
            )
        return self._schemas[key]

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #

    def validate(
        self,
        ref: SchemaRef,
        instance: dict[str, Any],
        source_file: Path | None = None,
    ) -> None:
        """校验实例，失败时抛 ``ValidationError``。

        ``source_file`` 用于错误定位；为 ``None`` 时标记为 ``<inline>``。
        """
        result = self.validate_with_result(ref, instance, source_file)
        if not result.valid:
            first = result.issues[0]
            raise ValidationError(
                f"Schema 校验失败 [{ref.file_stem}]: {first.message}",
                context={
                    "file_path": first.file_path,
                    "field_path": first.field_path,
                    "message": first.message,
                    "schema_id": first.schema_id,
                    "all_issues": [issue.model_dump() for issue in result.issues],
                },
            )

    def validate_file(self, file_path: Path, ref: SchemaRef) -> dict[str, Any]:
        """加载并校验文件，返回解析后的字典。失败时抛 ``ValidationError``。"""
        instance = YamlLoader.load(file_path)
        self.validate(ref, instance, file_path)
        return instance

    def validate_with_result(
        self,
        ref: SchemaRef,
        instance: dict[str, Any],
        source_file: Path | None = None,
    ) -> SchemaValidationResult:
        """校验实例，返回 ``SchemaValidationResult``（不抛异常）。"""
        key = ref.file_stem
        if key not in self._validators:
            raise ArgumentError(
                f"未加载的 Schema '{key}'，已知: {sorted(self._schemas.keys())}"
            )
        validator = self._validators[key]
        schema_id = self._schemas[key].get("$id")
        file_path_str = str(source_file) if source_file is not None else "<inline>"

        issues: list[SchemaValidationIssue] = []
        for error in sorted(validator.iter_errors(instance), key=lambda e: e.json_path):
            issues.append(
                SchemaValidationIssue(
                    file_path=file_path_str,
                    field_path=_resolve_field_path(error),
                    message=error.message,
                    schema_id=schema_id,
                )
            )
        if issues:
            return SchemaValidationResult.fail(ref, issues)
        return SchemaValidationResult.ok(ref)


__all__ = [
    "SchemaLoader",
    "YamlLoader",
]
