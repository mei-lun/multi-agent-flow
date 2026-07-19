"""TASK-011 契约测试：MAF 协议 Schema 加载与校验。

验收标准：
1. 合法模板通过；未知协议版本拒绝。
2. 缺字段、额外字段和错误枚举返回文件路径与字段路径。
3. Schema 校验有固定样例测试。

与《GitHub 分布式协作协议》第 3 节权威目录、第 5 节状态机对齐。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# packages/artifact_schemas/src 尚未加入 pyproject.toml pythonpath（TASK-002 范围），
# 此处显式添加，使 maf_artifact_schemas 可被 maf_server.git_coordination.schemas 导入。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT_SCHEMAS_SRC = _PROJECT_ROOT / "packages" / "artifact_schemas" / "src"
if str(_ARTIFACT_SCHEMAS_SRC) not in sys.path:
    sys.path.insert(0, str(_ARTIFACT_SCHEMAS_SRC))

from maf_artifact_schemas.protocol import (  # noqa: E402
    ProtocolVersion,
    SchemaRef,
)
from maf_domain.errors import ArgumentError, ValidationError  # noqa: E402
from maf_server.git_coordination.schemas import SchemaLoader, YamlLoader  # noqa: E402

# --------------------------------------------------------------------------- #
# 固定样例：与 templates/git_coordination/schemas/*.schema.json 对齐
# --------------------------------------------------------------------------- #

TEMPLATES_DIR = _PROJECT_ROOT / "templates" / "git_coordination"
SCHEMAS_DIR = TEMPLATES_DIR / "schemas"

TASK_REF = SchemaRef(name="task", version=1)
EVENT_REF = SchemaRef(name="event", version=1)
NODE_REF = SchemaRef(name="node", version=1)
PROJECT_REF = SchemaRef(name="project", version=1)


def _valid_task() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "task_id": "TASK-001",
        "title": "Implement feature X",
        "status": "PLANNED",
        "requirements": {"summary": "do X"},
        "dependencies": [],
        "assignment": None,
        "progress": {},
        "delivery": {},
        "version": 1,
    }


def _valid_event() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event_id": "evt-20260101-0001-abcdef",
        "event_type": "CLAIM_REQUESTED",
        "node_id": "node-abc123",
        "task_id": "TASK-001",
        "assignment_id": None,
        "assignment_epoch": None,
        "based_on_control_commit": "abc1234def",
        "occurred_at": "2026-01-01T00:00:00Z",
        "payload": {"note": "claim"},
    }


def _valid_node() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "node_id": "node-abc123",
        "display_name": "Runner 1",
        "git_identity": {"name": "runner", "email": "r@example.com"},
        "capabilities": ["code", "docs"],
        "capacity": 2,
        "status": "ACTIVE",
        "version": 1,
    }


def _valid_project() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project_id": "demo-project",
        "control_branch": "maf/control",
        "default_branch": "main",
        "coordination_mode": "git_single_writer",
        "task_schema": "task-v1",
        "node_schema": "node-v1",
        "event_schema": "event-v1",
        "progress_interval_minutes": 15,
        "assignment_timeout_minutes": 60,
        "assignment_grace_minutes": 15,
    }


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def loader() -> SchemaLoader:
    return SchemaLoader(SCHEMAS_DIR)


@pytest.fixture
def tmp_task_file(tmp_path: Path) -> Path:
    """临时任务文件路径，用于错误定位测试。"""
    return tmp_path / "TASK-001.yaml"


# --------------------------------------------------------------------------- #
# 验收标准 1：合法模板通过；未知协议版本拒绝
# --------------------------------------------------------------------------- #


class TestValidTemplatesPass:
    """合法样例应通过校验。"""

    def test_valid_task_passes(self, loader: SchemaLoader) -> None:
        loader.validate(TASK_REF, _valid_task())

    def test_valid_event_passes(self, loader: SchemaLoader) -> None:
        loader.validate(EVENT_REF, _valid_event())

    def test_valid_node_passes(self, loader: SchemaLoader) -> None:
        loader.validate(NODE_REF, _valid_node())

    def test_valid_project_passes(self, loader: SchemaLoader) -> None:
        loader.validate(PROJECT_REF, _valid_project())

    def test_project_yaml_template_passes(self, loader: SchemaLoader) -> None:
        """templates/git_coordination/project.yaml 自身应通过 project-v1 校验。"""
        instance = YamlLoader.load(TEMPLATES_DIR / "project.yaml")
        loader.validate(PROJECT_REF, instance)

    def test_all_protocol_event_types_pass(self, loader: SchemaLoader) -> None:
        """协议第 5/6/8/9 节定义的全部事件类型都应通过 event-v1 校验。"""
        base = _valid_event()
        for event_type in (
            "NODE_REGISTERED",
            "NODE_UPDATED",
            "CLAIM_REQUESTED",
            "PROGRESS_REPORTED",
            "BLOCKED_REPORTED",
            "SUBMISSION_CREATED",
            "WORK_ABANDONED",
        ):
            instance = {**base, "event_type": event_type}
            loader.validate(EVENT_REF, instance)

    def test_all_task_statuses_pass(self, loader: SchemaLoader) -> None:
        """协议第 5 节状态机的全部状态都应通过 task-v1 校验。"""
        base = _valid_task()
        for status in (
            "PLANNED", "READY", "ASSIGNED", "IN_PROGRESS", "BLOCKED",
            "SUBMITTED", "REVIEWING", "REWORK_REQUIRED", "LEASE_EXPIRED",
            "DONE", "FAILED", "CANCELLED",
        ):
            instance = {**base, "status": status}
            loader.validate(TASK_REF, instance)


class TestUnknownProtocolVersionRejected:
    """未知协议版本应被拒绝。"""

    def test_task_with_unknown_version_rejected(self, loader: SchemaLoader) -> None:
        instance = {**_valid_task(), "schema_version": 99}
        with pytest.raises(ValidationError) as exc_info:
            loader.validate(TASK_REF, instance)
        assert exc_info.value.context["field_path"] == "$.schema_version"

    def test_event_with_unknown_version_rejected(self, loader: SchemaLoader) -> None:
        instance = {**_valid_event(), "schema_version": 2}
        with pytest.raises(ValidationError) as exc_info:
            loader.validate(EVENT_REF, instance)
        assert exc_info.value.context["field_path"] == "$.schema_version"

    def test_protocol_version_from_value_rejects_unknown(self) -> None:
        with pytest.raises(ValueError):
            ProtocolVersion.from_value(99)

    def test_loader_rejects_unknown_schema_version_file(self, tmp_path: Path) -> None:
        """SchemaLoader 加载未知版本的 Schema 文件时应报错。"""
        bad_dir = tmp_path / "schemas"
        bad_dir.mkdir()
        (bad_dir / "task-v99.schema.json").write_text(
            '{"$id": "maf/task-v99", "type": "object", "properties": {}}',
            encoding="utf-8",
        )
        with pytest.raises(ArgumentError, match="未知协议版本"):
            SchemaLoader(bad_dir)


# --------------------------------------------------------------------------- #
# 验收标准 2：缺字段、额外字段和错误枚举返回文件路径与字段路径
# --------------------------------------------------------------------------- #


class TestMissingFieldReportsPaths:
    """缺字段时错误应包含文件路径与字段路径。"""

    def test_missing_task_id(
        self, loader: SchemaLoader, tmp_task_file: Path
    ) -> None:
        instance = _valid_task()
        del instance["task_id"]
        with pytest.raises(ValidationError) as exc_info:
            loader.validate(TASK_REF, instance, source_file=tmp_task_file)
        ctx = exc_info.value.context
        assert ctx["file_path"] == str(tmp_task_file)
        assert ctx["field_path"] == "$.task_id"
        assert "task_id" in ctx["message"] or "required" in ctx["message"].lower()

    def test_missing_event_id(self, loader: SchemaLoader) -> None:
        instance = _valid_event()
        del instance["event_id"]
        source = Path("events/evt.json")
        with pytest.raises(ValidationError) as exc_info:
            loader.validate(EVENT_REF, instance, source_file=source)
        ctx = exc_info.value.context
        assert ctx["file_path"] == str(source)
        assert ctx["field_path"] == "$.event_id"

    def test_missing_node_status(self, loader: SchemaLoader) -> None:
        instance = _valid_node()
        del instance["status"]
        with pytest.raises(ValidationError) as exc_info:
            loader.validate(NODE_REF, instance)
        ctx = exc_info.value.context
        assert ctx["field_path"] == "$.status"

    def test_missing_project_control_branch(self, loader: SchemaLoader) -> None:
        instance = _valid_project()
        del instance["control_branch"]
        with pytest.raises(ValidationError) as exc_info:
            loader.validate(PROJECT_REF, instance)
        ctx = exc_info.value.context
        assert ctx["field_path"] == "$.control_branch"


class TestExtraFieldReportsPaths:
    """额外字段时错误应包含文件路径与字段路径。"""

    def test_task_extra_field(
        self, loader: SchemaLoader, tmp_task_file: Path
    ) -> None:
        instance = _valid_task()
        instance["unexpected_field"] = "surprise"
        with pytest.raises(ValidationError) as exc_info:
            loader.validate(TASK_REF, instance, source_file=tmp_task_file)
        ctx = exc_info.value.context
        assert ctx["file_path"] == str(tmp_task_file)
        assert ctx["field_path"] == "$.unexpected_field"

    def test_event_extra_field(self, loader: SchemaLoader) -> None:
        instance = _valid_event()
        instance["bonus"] = 1
        with pytest.raises(ValidationError) as exc_info:
            loader.validate(EVENT_REF, instance)
        ctx = exc_info.value.context
        assert ctx["field_path"] == "$.bonus"

    def test_node_extra_field(self, loader: SchemaLoader) -> None:
        instance = _valid_node()
        instance["secret"] = "no"
        with pytest.raises(ValidationError) as exc_info:
            loader.validate(NODE_REF, instance)
        ctx = exc_info.value.context
        assert ctx["field_path"] == "$.secret"


class TestWrongEnumReportsPaths:
    """错误枚举值时错误应包含文件路径与字段路径。"""

    def test_task_wrong_status(
        self, loader: SchemaLoader, tmp_task_file: Path
    ) -> None:
        instance = {**_valid_task(), "status": "INVALID"}
        with pytest.raises(ValidationError) as exc_info:
            loader.validate(TASK_REF, instance, source_file=tmp_task_file)
        ctx = exc_info.value.context
        assert ctx["file_path"] == str(tmp_task_file)
        assert ctx["field_path"] == "$.status"
        assert "INVALID" in ctx["message"]

    def test_event_wrong_type(self, loader: SchemaLoader) -> None:
        instance = {**_valid_event(), "event_type": "PROGRESS"}
        with pytest.raises(ValidationError) as exc_info:
            loader.validate(EVENT_REF, instance)
        ctx = exc_info.value.context
        assert ctx["field_path"] == "$.event_type"

    def test_node_wrong_status(self, loader: SchemaLoader) -> None:
        instance = {**_valid_node(), "status": "BUSY"}
        with pytest.raises(ValidationError) as exc_info:
            loader.validate(NODE_REF, instance)
        ctx = exc_info.value.context
        assert ctx["field_path"] == "$.status"

    def test_project_wrong_coordination_mode(self, loader: SchemaLoader) -> None:
        instance = {**_valid_project(), "coordination_mode": "http"}
        with pytest.raises(ValidationError) as exc_info:
            loader.validate(PROJECT_REF, instance)
        ctx = exc_info.value.context
        assert ctx["field_path"] == "$.coordination_mode"


# --------------------------------------------------------------------------- #
# 验收标准 3：Schema 校验有固定样例测试（validate_file 集成）
# --------------------------------------------------------------------------- #


class TestValidateFileIntegration:
    """validate_file 端到端测试：从磁盘加载并校验。"""

    def test_validate_valid_task_file(
        self, loader: SchemaLoader, tmp_task_file: Path
    ) -> None:
        import yaml as pyyaml

        tmp_task_file.write_text(
            pyyaml.safe_dump(_valid_task(), sort_keys=False),
            encoding="utf-8",
        )
        result = loader.validate_file(tmp_task_file, TASK_REF)
        assert result["task_id"] == "TASK-001"

    def test_validate_invalid_task_file(
        self, loader: SchemaLoader, tmp_task_file: Path
    ) -> None:
        import yaml as pyyaml

        bad = _valid_task()
        del bad["task_id"]
        tmp_task_file.write_text(pyyaml.safe_dump(bad, sort_keys=False), encoding="utf-8")
        with pytest.raises(ValidationError) as exc_info:
            loader.validate_file(tmp_task_file, TASK_REF)
        ctx = exc_info.value.context
        assert ctx["file_path"] == str(tmp_task_file)
        assert ctx["field_path"] == "$.task_id"

    def test_yaml_loader_rejects_non_dict(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "list.yaml"
        bad_file.write_text("- item1\n- item2\n", encoding="utf-8")
        with pytest.raises(ArgumentError, match="不是对象"):
            YamlLoader.load(bad_file)

    def test_yaml_loader_rejects_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ArgumentError, match="文件不存在"):
            YamlLoader.load(tmp_path / "nope.yaml")


# --------------------------------------------------------------------------- #
# SchemaLoader 自身行为
# --------------------------------------------------------------------------- #


class TestSchemaLoaderBehavior:
    """SchemaLoader 加载与查询行为。"""

    def test_known_refs_include_all_schemas(self, loader: SchemaLoader) -> None:
        refs = loader.known_refs()
        names = {ref.file_stem for ref in refs}
        assert "task-v1" in names
        assert "event-v1" in names
        assert "node-v1" in names
        assert "project-v1" in names

    def test_get_schema_returns_dict(self, loader: SchemaLoader) -> None:
        schema = loader.get_schema(TASK_REF)
        assert schema["$id"] == "maf/task-v1"
        assert schema["type"] == "object"

    def test_get_schema_unknown_ref_raises(self, loader: SchemaLoader) -> None:
        unknown = SchemaRef(name="task", version=99)
        with pytest.raises(ArgumentError, match="未加载的 Schema"):
            loader.get_schema(unknown)

    def test_validate_unknown_ref_raises(self, loader: SchemaLoader) -> None:
        unknown = SchemaRef(name="task", version=99)
        with pytest.raises(ArgumentError, match="未加载的 Schema"):
            loader.validate(unknown, {})

    def test_validate_with_result_returns_issues(self, loader: SchemaLoader) -> None:
        instance = {**_valid_task(), "status": "NOPE"}
        result = loader.validate_with_result(TASK_REF, instance)
        assert not result.valid
        assert len(result.issues) >= 1
        assert result.issues[0].field_path == "$.status"

    def test_validate_with_result_ok(self, loader: SchemaLoader) -> None:
        result = loader.validate_with_result(TASK_REF, _valid_task())
        assert result.valid
        assert result.issues == []
