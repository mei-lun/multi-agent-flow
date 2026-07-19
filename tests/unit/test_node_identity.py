"""TASK-013 单元测试：节点 ID 持久化与本地清单。

验收标准：

1. 重启后 node_id 不变化（``load_or_create_node_id`` 持久化）。
2. 不读取硬件序列号生成身份（仅使用 ``uuid4``）。
3. 清单通过 ``node-v1`` Schema（由 ``SchemaLoader`` 校验）。
4. 注册事件可生成并携带必要字段（NODE_REGISTERED → NODE_UPDATED）。
5. 节点 ID 唯一：不同 workspace 生成不同 ID；多次调用 ``_generate_node_id`` 不重复。

与《GitHub 分布式协作协议》§4 节点身份、§6 任务认领、§7 防止旧节点覆盖对齐。
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

# packages/artifact_schemas/src 不在 pyproject.toml 的 pythonpath 中（TASK-002 范围），
# 这里显式添加，使 maf_server.git_coordination.schemas 可导入以做 Schema 校验。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT_SCHEMAS_SRC = _PROJECT_ROOT / "packages" / "artifact_schemas" / "src"
if str(_ARTIFACT_SCHEMAS_SRC) not in sys.path:
    sys.path.insert(0, str(_ARTIFACT_SCHEMAS_SRC))

from maf_artifact_schemas.protocol import SchemaRef  # noqa: E402
from maf_runner.config import NodeSettings  # noqa: E402
from maf_runner.registry import (  # noqa: E402
    NODE_ID_PATTERN,
    RunnerRegistry,
    _generate_node_id,
    _is_valid_node_id,
    load_or_create_node_id,
)
from maf_server.git_coordination.schemas import SchemaLoader  # noqa: E402

# --------------------------------------------------------------------------- #
# 固定常量
# --------------------------------------------------------------------------- #

_TEMPLATES_DIR = _PROJECT_ROOT / "templates" / "git_coordination"
_SCHEMAS_DIR = _TEMPLATES_DIR / "schemas"
_NODE_REF = SchemaRef(name="node", version=1)
_EVENT_REF = SchemaRef(name="event", version=1)

_VALID_NODE_ID = "node-12345678-1234-1234-1234-123456789abc"
_VALID_CONTROL_COMMIT = "abcdef1234567890abcdef1234567890abcdef12"
#: NodeSettings.software_version 的默认值（与 config._DEFAULT_SOFTWARE_VERSION 对齐）。
_EXPECTED_DEFAULT_SOFTWARE_VERSION = "maf-runner-0.0.0"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_maf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除所有 ``MAF_*`` 环境变量，避免本地 .env 污染测试。"""
    for key in list(os.environ):
        if key.startswith("MAF_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(scope="module")
def loader() -> SchemaLoader:
    return SchemaLoader(_SCHEMAS_DIR)


class _StaticGitIdentity:
    """测试用 ``GitIdentityProvider`` 实现，返回固定 name/email。"""

    def __init__(self, name: str = "runner-bot", email: str = "runner@example.com") -> None:
        self._identity = {"name": name, "email": email}

    def read_identity(self) -> dict[str, str]:
        return dict(self._identity)


def _node_kwargs(tmp_path: Path, **overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = dict(
        node_id=_VALID_NODE_ID,
        control_remote_url="origin",
        workspace_root=tmp_path,
        model_mapping_path=tmp_path / "model-mapping.yaml",
        capability_token_cache_path=Path("capability-tokens.db"),
        _env_file=None,
    )
    kwargs.update(overrides)
    return kwargs


def _make_node(tmp_path: Path, **overrides: object) -> NodeSettings:
    return NodeSettings(**_node_kwargs(tmp_path, **overrides))


def _make_registry(
    tmp_path: Path, **overrides: object
) -> RunnerRegistry:
    settings = _make_node(tmp_path, **overrides)
    return RunnerRegistry(settings=settings)


# --------------------------------------------------------------------------- #
# 验收 1：重启后 node_id 不变化
# --------------------------------------------------------------------------- #


class TestNodeIdPersistence:
    """``load_or_create_node_id`` 必须持久化 node_id。"""

    def test_generates_new_id_when_no_env_no_file(self, tmp_path: Path) -> None:
        node_id = load_or_create_node_id(tmp_path, env_node_id=None)
        assert _is_valid_node_id(node_id)
        state_file = tmp_path / ".maf" / "node-id"
        assert state_file.exists()
        assert state_file.read_text(encoding="utf-8").strip() == node_id

    def test_same_id_across_calls_restart_stable(self, tmp_path: Path) -> None:
        first = load_or_create_node_id(tmp_path, env_node_id=None)
        # 模拟“重启”：同一 workspace_root 再次调用，不传 env。
        second = load_or_create_node_id(tmp_path, env_node_id=None)
        third = load_or_create_node_id(tmp_path, env_node_id=None)
        assert first == second == third

    def test_env_node_id_persists_and_overrides_file(self, tmp_path: Path) -> None:
        env_id = "node-abcdef12-3456-7890-abcd-ef1234567890"
        result = load_or_create_node_id(tmp_path, env_node_id=env_id)
        assert result == env_id
        state_file = tmp_path / ".maf" / "node-id"
        assert state_file.read_text(encoding="utf-8").strip() == env_id
        # 后续无 env 调用应读取已持久化的值。
        assert load_or_create_node_id(tmp_path, env_node_id=None) == env_id

    def test_invalid_env_falls_back_to_file_or_generate(self, tmp_path: Path) -> None:
        # 无效 env（不含合法 UUID）不应被使用。
        result = load_or_create_node_id(tmp_path, env_node_id="not-a-valid-id")
        assert result != "not-a-valid-id"
        assert _is_valid_node_id(result)

    def test_invalid_env_falls_back_to_existing_file(self, tmp_path: Path) -> None:
        # 预先写入文件，无效 env 时应回退到文件内容。
        existing = "node-11112222-3333-4444-5555-666677778888"
        state_file = tmp_path / ".maf" / "node-id"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(existing + "\n", encoding="utf-8")
        result = load_or_create_node_id(tmp_path, env_node_id="garbage")
        assert result == existing

    def test_corrupted_file_regenerates(self, tmp_path: Path) -> None:
        state_file = tmp_path / ".maf" / "node-id"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("corrupted-content\n", encoding="utf-8")
        result = load_or_create_node_id(tmp_path, env_node_id=None)
        assert result != "corrupted-content"
        assert _is_valid_node_id(result)
        # 文件被覆盖为新 ID。
        assert state_file.read_text(encoding="utf-8").strip() == result

    def test_state_dir_path_is_under_workspace_root(self, tmp_path: Path) -> None:
        load_or_create_node_id(tmp_path, env_node_id=None)
        assert (tmp_path / ".maf" / "node-id").exists()
        # 确认 .maf 目录在 workspace_root 下，符合 confinement 约束。
        assert (tmp_path / ".maf").is_dir()

    def test_custom_state_location(self, tmp_path: Path) -> None:
        custom_dir = "node-state"
        custom_file = "id.txt"
        result = load_or_create_node_id(
            tmp_path,
            env_node_id=None,
            state_dir_name=custom_dir,
            state_file_name=custom_file,
        )
        assert (tmp_path / custom_dir / custom_file).exists()
        assert (tmp_path / custom_dir / custom_file).read_text(
            encoding="utf-8"
        ).strip() == result


# --------------------------------------------------------------------------- #
# 验收 2：不读取硬件序列号生成身份
# --------------------------------------------------------------------------- #


class TestNodeIdGenerationNoHardware:
    """node_id 必须由随机 UUID 生成，不依赖硬件指纹。"""

    def test_generated_id_is_uuid4_format(self) -> None:
        node_id = _generate_node_id()
        assert node_id.startswith("node-")
        suffix = node_id[len("node-"):]
        parsed = uuid.UUID(suffix)
        # uuid4 生成时设置 version=4，variant=RFC 4122。
        assert parsed.version == 4

    def test_generated_id_matches_schema_pattern(self) -> None:
        import re

        node_id = _generate_node_id()
        assert re.match(NODE_ID_PATTERN, node_id)

    def test_two_generations_produce_different_ids(self) -> None:
        ids = {_generate_node_id() for _ in range(20)}
        assert len(ids) == 20

    def test_load_or_create_generates_unique_per_workspace(
        self, tmp_path: Path
    ) -> None:
        seen: set[str] = set()
        for i in range(5):
            ws = tmp_path / f"ws-{i}"
            ws.mkdir()
            seen.add(load_or_create_node_id(ws, env_node_id=None))
        assert len(seen) == 5

    def test_no_platform_modules_imported_for_identity(self) -> None:
        """``registry`` 模块不应导入 platform/socket/uuid.getnode 等硬件 API。"""
        import maf_runner.registry as reg_mod

        source = open(reg_mod.__file__, encoding="utf-8").read()
        # 禁止使用硬件序列号相关 API（协议 §4）。
        forbidden = ["getnode", "uuid.getnode", "platform.system", "socket.if_name"]
        for token in forbidden:
            assert token not in source, (
                f"registry.py 不应使用硬件序列号 API: {token}"
            )


# --------------------------------------------------------------------------- #
# 验收 3：清单通过 node-v1 Schema
# --------------------------------------------------------------------------- #


class TestManifestPassesNodeSchema:
    """``RunnerRegistry.build_manifest`` 输出必须通过 ``node-v1`` Schema。"""

    def test_manifest_passes_schema(self, loader: SchemaLoader, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        manifest = registry.build_manifest()
        loader.validate(_NODE_REF, dict(manifest))

    def test_manifest_has_all_required_fields(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        manifest = registry.build_manifest()
        for key in (
            "schema_version",
            "node_id",
            "display_name",
            "git_identity",
            "capabilities",
            "capacity",
            "status",
            "version",
        ):
            assert key in manifest, f"missing required field: {key}"

    def test_manifest_includes_optional_fields(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        manifest = registry.build_manifest()
        for key in (
            "model_aliases",
            "docker_profiles",
            "software_version",
            "generated_at",
        ):
            assert key in manifest, f"missing optional field: {key}"

    def test_schema_version_is_one(self, tmp_path: Path) -> None:
        manifest = _make_registry(tmp_path).build_manifest()
        assert manifest["schema_version"] == 1

    def test_node_id_matches_settings(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        manifest = registry.build_manifest()
        assert manifest["node_id"] == registry.settings.node_id
        assert manifest["node_id"] == _VALID_NODE_ID

    def test_capabilities_equal_labels(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path, labels=["python", "docker"])
        manifest = registry.build_manifest()
        assert manifest["capabilities"] == ["python", "docker"]

    def test_capacity_equal_max_concurrency(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path, max_concurrency=4)
        manifest = registry.build_manifest()
        assert manifest["capacity"] == 4

    def test_model_aliases_and_docker_profiles_propagated(
        self, tmp_path: Path
    ) -> None:
        registry = _make_registry(
            tmp_path,
            model_aliases=["gpt-4o", "glm-4"],
            docker_profiles=["python-slim", "node-slim"],
        )
        manifest = registry.build_manifest()
        assert manifest["model_aliases"] == ["gpt-4o", "glm-4"]
        assert manifest["docker_profiles"] == ["python-slim", "node-slim"]

    def test_display_name_falls_back_to_node_id(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)  # display_name 默认为空
        manifest = registry.build_manifest()
        assert manifest["display_name"] == _VALID_NODE_ID

    def test_display_name_uses_configured_value(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path, display_name="runner-1")
        manifest = registry.build_manifest()
        assert manifest["display_name"] == "runner-1"

    def test_software_version_defaults(self, tmp_path: Path) -> None:
        manifest = _make_registry(tmp_path).build_manifest()
        assert manifest["software_version"] == _EXPECTED_DEFAULT_SOFTWARE_VERSION

    def test_git_identity_uses_provider(self, tmp_path: Path) -> None:
        provider = _StaticGitIdentity(name="alice", email="alice@example.com")
        settings = _make_node(tmp_path)
        registry = RunnerRegistry(settings=settings, git_identity_provider=provider)
        manifest = registry.build_manifest()
        assert manifest["git_identity"] == {"name": "alice", "email": "alice@example.com"}

    def test_git_identity_falls_back_when_no_provider(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        manifest = registry.build_manifest()
        identity = manifest["git_identity"]
        assert "name" in identity
        assert "email" in identity
        assert identity["name"]
        assert identity["email"]

    def test_git_identity_falls_back_when_provider_returns_empty(
        self, tmp_path: Path
    ) -> None:
        class _Empty:
            def read_identity(self) -> dict[str, str]:
                return {}

        settings = _make_node(tmp_path)
        registry = RunnerRegistry(settings=settings, git_identity_provider=_Empty())
        manifest = registry.build_manifest()
        assert manifest["git_identity"]["name"]
        assert manifest["git_identity"]["email"]

    def test_status_defaults_to_active(self, tmp_path: Path) -> None:
        manifest = _make_registry(tmp_path).build_manifest()
        assert manifest["status"] == "ACTIVE"

    def test_status_can_be_overridden(self, tmp_path: Path) -> None:
        settings = _make_node(tmp_path)
        registry = RunnerRegistry(settings=settings, manifest_status="DRAINING")
        assert registry.build_manifest()["status"] == "DRAINING"

    def test_invalid_status_rejected(self, tmp_path: Path) -> None:
        settings = _make_node(tmp_path)
        with pytest.raises(ValueError, match="manifest_status"):
            RunnerRegistry(settings=settings, manifest_status="BUSY")

    def test_generated_at_is_iso8601(self, tmp_path: Path) -> None:
        import re

        manifest = _make_registry(tmp_path).build_manifest()
        # 形如 2026-01-01T00:00:00Z
        assert re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", manifest["generated_at"]
        )

    def test_manifest_passes_schema_with_all_fields(
        self, loader: SchemaLoader, tmp_path: Path
    ) -> None:
        registry = _make_registry(
            tmp_path,
            labels=["python", "docker"],
            model_aliases="gpt-4o,glm-4",
            docker_profiles="python-slim",
            display_name="runner-prod-1",
            max_concurrency=8,
        )
        manifest = registry.build_manifest()
        loader.validate(_NODE_REF, dict(manifest))
        assert manifest["capabilities"] == ["python", "docker"]
        assert manifest["model_aliases"] == ["gpt-4o", "glm-4"]
        assert manifest["docker_profiles"] == ["python-slim"]
        assert manifest["display_name"] == "runner-prod-1"
        assert manifest["capacity"] == 8


# --------------------------------------------------------------------------- #
# 验收 4：注册事件生成
# --------------------------------------------------------------------------- #


class TestRegistrationEvent:
    """``build_registration_event`` 必须生成合法事件。"""

    def test_first_call_is_node_registered(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        manifest = registry.build_manifest()
        event = registry.build_registration_event(manifest, _VALID_CONTROL_COMMIT)
        assert event["event_type"] == "NODE_REGISTERED"

    def test_second_call_is_node_updated(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        manifest = registry.build_manifest()
        registry.build_registration_event(manifest, _VALID_CONTROL_COMMIT)
        event2 = registry.build_registration_event(manifest, _VALID_CONTROL_COMMIT)
        assert event2["event_type"] == "NODE_UPDATED"

    def test_event_carries_node_id(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        manifest = registry.build_manifest()
        event = registry.build_registration_event(manifest, _VALID_CONTROL_COMMIT)
        assert event["node_id"] == _VALID_NODE_ID
        assert event["node_id"] == manifest["node_id"]

    def test_event_carries_control_commit(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        manifest = registry.build_manifest()
        event = registry.build_registration_event(manifest, _VALID_CONTROL_COMMIT)
        assert event["based_on_control_commit"] == _VALID_CONTROL_COMMIT

    def test_event_payload_contains_manifest(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        manifest = registry.build_manifest()
        event = registry.build_registration_event(manifest, _VALID_CONTROL_COMMIT)
        assert event["payload"]["manifest"]["node_id"] == _VALID_NODE_ID
        assert event["payload"]["manifest"]["schema_version"] == 1

    def test_event_id_is_unique(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        manifest = registry.build_manifest()
        e1 = registry.build_registration_event(manifest, _VALID_CONTROL_COMMIT)
        e2 = registry.build_registration_event(manifest, _VALID_CONTROL_COMMIT)
        assert e1["event_id"] != e2["event_id"]
        assert e1["event_id"].startswith("evt-")
        assert e2["event_id"].startswith("evt-")

    def test_event_has_no_task_assignment(self, tmp_path: Path) -> None:
        # 注册事件不绑定任务（协议 §6）。
        registry = _make_registry(tmp_path)
        manifest = registry.build_manifest()
        event = registry.build_registration_event(manifest, _VALID_CONTROL_COMMIT)
        assert event["task_id"] is None
        assert event["assignment_id"] is None
        assert event["assignment_epoch"] is None

    def test_event_occurred_at_is_iso8601(self, tmp_path: Path) -> None:
        import re

        registry = _make_registry(tmp_path)
        manifest = registry.build_manifest()
        event = registry.build_registration_event(manifest, _VALID_CONTROL_COMMIT)
        assert re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", event["occurred_at"]
        )

    def test_empty_control_commit_rejected(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        manifest = registry.build_manifest()
        with pytest.raises(ValueError, match="control_commit"):
            registry.build_registration_event(manifest, "")

    def test_short_control_commit_rejected(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        manifest = registry.build_manifest()
        with pytest.raises(ValueError, match="control_commit"):
            registry.build_registration_event(manifest, "abc")

    def test_event_passes_event_v1_schema(
        self, loader: SchemaLoader, tmp_path: Path
    ) -> None:
        registry = _make_registry(tmp_path)
        manifest = registry.build_manifest()
        event = registry.build_registration_event(manifest, _VALID_CONTROL_COMMIT)
        loader.validate(_EVENT_REF, dict(event))


# --------------------------------------------------------------------------- #
# 验收 5：节点 ID 唯一性
# --------------------------------------------------------------------------- #


class TestNodeIdUniqueness:
    """不同节点的 node_id 必须唯一。"""

    def test_different_workspaces_different_ids(self, tmp_path: Path) -> None:
        ws1 = tmp_path / "ws1"
        ws2 = tmp_path / "ws2"
        ws1.mkdir()
        ws2.mkdir()
        id1 = load_or_create_node_id(ws1, env_node_id=None)
        id2 = load_or_create_node_id(ws2, env_node_id=None)
        assert id1 != id2

    def test_two_regries_have_independent_node_ids(self, tmp_path: Path) -> None:
        ws1 = tmp_path / "ws1"
        ws2 = tmp_path / "ws2"
        ws1.mkdir()
        ws2.mkdir()
        id1 = load_or_create_node_id(ws1, env_node_id=None)
        id2 = load_or_create_node_id(ws2, env_node_id=None)
        settings1 = _make_node(ws1, node_id=id1)
        settings2 = _make_node(ws2, node_id=id2)
        r1 = RunnerRegistry(settings=settings1)
        r2 = RunnerRegistry(settings=settings2)
        assert r1.node_id != r2.node_id

    def test_node_id_property_matches_settings(self, tmp_path: Path) -> None:
        registry = _make_registry(tmp_path)
        assert registry.node_id == _VALID_NODE_ID
