"""TASK-048 契约测试：Tool 注册表。

验收标准：
1. ``ToolAdapter`` Protocol 定义清晰，含 metadata 字段与 invoke 签名（不实现）。
2. ``register_tool`` 仅 DESIGNER/ADMIN 可调用；其他角色抛 PermissionDenied。
3. 同名同版本重复注册抛 AlreadyExistsError。
4. 注册后可通过 ``list_tools`` / ``get_tool`` 查询。
5. 注册触发 ToolRegistered 事件（落 outbox_events）。
6. 新版本注册保留旧版本；``list_versions`` 返回全部版本。
7. ``unregister_tool`` 仅 ADMIN 可调用；保留同名其他版本。
8. 注册过程不执行 Tool（不调用 invoke / cancel）。

测试范围：
- ``packages/tool_adapters/src/maf_tool_adapters/{base,echo}.py``
- ``apps/server/src/maf_server/modules/tools/{schemas,repository,service}.py``
- ``apps/server/src/maf_server/modules/tools/router.py``（HTTP 契约）
- 不测试 Tool 调用（TASK-051 范围）、不测试 MCP 同步（TASK-049 范围）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from maf_contracts.common import ActorContext
from maf_domain.errors import (
    AlreadyExistsError,
    ArgumentError,
    ErrorCode,
    NotFoundError,
    PermissionDeniedError,
)
from maf_policy import CasbinPermissionService
from maf_server.config import ServerSettings
from maf_server.core.database import Database
from maf_server.core.events import SqliteOutboxRepository
from maf_tool_adapters import EchoToolAdapter, ToolAdapter, ToolMetadata

from maf_server.modules.tools import (
    ToolRegistryService,
    build_tools_router,
    init_tool_registry_schema_on_database,
)


_SECRET_PLAINTEXT = "test-secret-for-tool-registry-task-048"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_maf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove any ``MAF_*`` env vars so tests start from a clean slate."""
    for key in list(os.environ):
        if key.startswith("MAF_"):
            monkeypatch.delenv(key, raising=False)


def _make_settings(tmp_path: Path, **overrides: object) -> ServerSettings:
    kwargs: dict[str, object] = dict(
        organization_id="org-001",
        business_db_path=Path("maf.db"),
        checkpointer_db_path=Path("checkpoints.db"),
        artifact_root=Path("artifacts"),
        workspace_root=Path("workspaces"),
        git_repo_root=tmp_path / "repo",
        public_base_url="http://localhost:8000",
        secret_key=_SECRET_PLAINTEXT,
        data_dir=tmp_path,
        _env_file=None,
    )
    kwargs.update(overrides)
    return ServerSettings(**kwargs)


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    """已初始化并建好 tools/outbox 表的 Database。"""
    settings = _make_settings(tmp_path)
    database = Database(settings)
    await database.initialize()
    await init_tool_registry_schema_on_database(database)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def service(db: Database) -> ToolRegistryService:
    """ToolRegistryService，使用 CasbinPermissionService 默认策略。"""
    return ToolRegistryService(db)


def _actor(user_id: str, roles: list[str], trace_id: str = "tool-trace") -> ActorContext:
    """构造测试用 ActorContext。"""
    return ActorContext(
        user_id=user_id,
        organization_id="org-001",
        permission_keys=roles,
        trace_id=trace_id,
    )


# --------------------------------------------------------------------------- #
# 验收 1：ToolAdapter Protocol 契约
# --------------------------------------------------------------------------- #


class TestToolAdapterProtocol:
    """``ToolAdapter`` Protocol 与 ``ToolMetadata`` dataclass 契约。"""

    def test_tool_metadata_fields(self) -> None:
        """ToolMetadata 含全部必需字段。"""
        meta = ToolMetadata(
            name="echo",
            version="1.0.0",
            description="echo tool",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            capabilities=["echo"],
            adapter_type="NATIVE",
        )
        assert meta.name == "echo"
        assert meta.version == "1.0.0"
        assert meta.description == "echo tool"
        assert meta.input_schema == {"type": "object"}
        assert meta.output_schema == {"type": "object"}
        assert meta.capabilities == ["echo"]
        assert meta.adapter_type == "NATIVE"

    def test_tool_metadata_defaults(self) -> None:
        """ToolMetadata 默认值合理。"""
        meta = ToolMetadata(name="t", version="1.0.0")
        assert meta.description == ""
        assert meta.input_schema == {}
        assert meta.output_schema == {}
        assert meta.capabilities == []
        assert meta.adapter_type == "NATIVE"

    def test_echo_adapter_is_tool_adapter(self) -> None:
        """EchoToolAdapter 实现 ToolAdapter Protocol。"""
        adapter = EchoToolAdapter()
        # runtime_checkable Protocol 支持 isinstance 检查
        assert isinstance(adapter, ToolAdapter)

    def test_echo_adapter_metadata(self) -> None:
        """EchoToolAdapter.metadata 返回 ToolMetadata。"""
        adapter = EchoToolAdapter()
        meta = adapter.metadata
        assert isinstance(meta, ToolMetadata)
        assert meta.name == "echo"
        assert meta.version == "1.0.0"
        assert meta.adapter_type == "NATIVE"
        assert "echo" in meta.capabilities
        assert "type" in meta.input_schema
        assert "type" in meta.output_schema

    def test_echo_adapter_has_invoke_and_cancel(self) -> None:
        """ToolAdapter 声明 invoke 与 cancel 签名。"""
        adapter = EchoToolAdapter()
        assert callable(getattr(adapter, "invoke", None))
        assert callable(getattr(adapter, "cancel", None))

    def test_echo_adapter_custom_metadata(self) -> None:
        """EchoToolAdapter 可自定义 name/version/capabilities。"""
        adapter = EchoToolAdapter(
            name="custom",
            version="2.0.0",
            capabilities=["a", "b"],
        )
        meta = adapter.metadata
        assert meta.name == "custom"
        assert meta.version == "2.0.0"
        assert meta.capabilities == ["a", "b"]


# --------------------------------------------------------------------------- #
# 验收 2 & 3 & 4：注册、查询、唯一性
# --------------------------------------------------------------------------- #


class TestRegisterAndQuery:
    """``register_tool`` / ``list_tools`` / ``get_tool`` 契约。"""

    @pytest.mark.asyncio
    async def test_designer_can_register_tool(self, service: ToolRegistryService) -> None:
        """DESIGNER 可注册 Tool。"""
        actor = _actor("designer-1", roles=["DESIGNER"])
        adapter = EchoToolAdapter()
        view = await service.register_tool(adapter, actor=actor)

        assert view["name"] == "echo"
        assert view["version"] == "1.0.0"
        assert view["adapter_type"] == "NATIVE"
        assert view["version_no"] == 1
        assert view["created_by"] == "designer-1"
        assert view["id"]  # UUID 非空
        assert view["created_at"]  # ISO 时间戳非空
        assert "echo" in view["capabilities"]
        assert view["input_schema"]["type"] == "object"

    @pytest.mark.asyncio
    async def test_admin_can_register_tool(self, service: ToolRegistryService) -> None:
        """ADMIN 可注册 Tool。"""
        actor = _actor("admin-1", roles=["ADMIN"])
        view = await service.register_tool(EchoToolAdapter(), actor=actor)
        assert view["name"] == "echo"
        assert view["version_no"] == 1

    @pytest.mark.asyncio
    async def test_observer_cannot_register_tool(
        self, service: ToolRegistryService
    ) -> None:
        """OBSERVER 不能注册 Tool（无 tools:write 权限）。"""
        actor = _actor("obs-1", roles=["OBSERVER"])
        with pytest.raises(PermissionDeniedError) as exc_info:
            await service.register_tool(EchoToolAdapter(), actor=actor)
        assert exc_info.value.error_code == ErrorCode.PERMISSION_DENIED

    @pytest.mark.asyncio
    async def test_observer_with_designer_can_register(
        self, service: ToolRegistryService
    ) -> None:
        """多角色 actor，DESIGNER 角色足以注册。"""
        actor = _actor("multi-1", roles=["OBSERVER", "DESIGNER"])
        view = await service.register_tool(EchoToolAdapter(), actor=actor)
        assert view["name"] == "echo"

    @pytest.mark.asyncio
    async def test_duplicate_name_version_raises_already_exists(
        self, service: ToolRegistryService
    ) -> None:
        """同名同版本重复注册抛 AlreadyExistsError（HTTP 409 语义）。"""
        actor = _actor("designer-1", roles=["DESIGNER"])
        await service.register_tool(EchoToolAdapter(), actor=actor)

        with pytest.raises(AlreadyExistsError) as exc_info:
            await service.register_tool(EchoToolAdapter(), actor=actor)
        assert exc_info.value.error_code == ErrorCode.ALREADY_EXISTS
        assert "echo" in exc_info.value.context.get("name", "")
        assert "1.0.0" in exc_info.value.context.get("version", "")

    @pytest.mark.asyncio
    async def test_new_version_preserves_old(
        self, service: ToolRegistryService
    ) -> None:
        """新版本注册保留旧版本。"""
        actor = _actor("designer-1", roles=["DESIGNER"])
        # v1.0.0
        v1 = await service.register_tool(EchoToolAdapter(), actor=actor)
        # v1.1.0
        v2 = await service.register_tool(
            EchoToolAdapter(version="1.1.0"), actor=actor
        )

        assert v1["version_no"] == 1
        assert v2["version_no"] == 2

        # 旧版本仍可查询
        v1_again = await service.get_tool("echo", "1.0.0")
        assert v1_again["id"] == v1["id"]
        assert v1_again["version_no"] == 1

        # 新版本可查询
        v2_again = await service.get_tool("echo", "1.1.0")
        assert v2_again["id"] == v2["id"]
        assert v2_again["version_no"] == 2

    @pytest.mark.asyncio
    async def test_list_tools_returns_all(
        self, service: ToolRegistryService
    ) -> None:
        """list_tools 返回全部已注册 Tool。"""
        actor = _actor("designer-1", roles=["DESIGNER"])
        await service.register_tool(EchoToolAdapter(), actor=actor)
        await service.register_tool(
            EchoToolAdapter(name="other", version="1.0.0"), actor=actor
        )

        result = await service.list_tools()
        names_versions = {(item["name"], item["version"]) for item in result["items"]}
        assert ("echo", "1.0.0") in names_versions
        assert ("other", "1.0.0") in names_versions
        assert len(result["items"]) == 2

    @pytest.mark.asyncio
    async def test_list_tools_empty_when_no_tools(
        self, service: ToolRegistryService
    ) -> None:
        """无 Tool 时 list_tools 返回空列表。"""
        result = await service.list_tools()
        assert result["items"] == []

    @pytest.mark.asyncio
    async def test_get_tool_not_found(self, service: ToolRegistryService) -> None:
        """get_tool 不存在抛 NotFoundError。"""
        with pytest.raises(NotFoundError) as exc_info:
            await service.get_tool("nonexistent", "1.0.0")
        assert exc_info.value.error_code == ErrorCode.NOT_FOUND

    @pytest.mark.asyncio
    async def test_list_versions(self, service: ToolRegistryService) -> None:
        """list_versions 返回指定 name 的全部版本，按 version_no 升序。"""
        actor = _actor("designer-1", roles=["DESIGNER"])
        await service.register_tool(EchoToolAdapter(version="1.0.0"), actor=actor)
        await service.register_tool(EchoToolAdapter(version="1.1.0"), actor=actor)
        await service.register_tool(EchoToolAdapter(version="2.0.0"), actor=actor)

        versions = await service.list_versions("echo")
        assert len(versions) == 3
        assert [v["version_no"] for v in versions] == [1, 2, 3]
        assert [v["version"] for v in versions] == ["1.0.0", "1.1.0", "2.0.0"]

    @pytest.mark.asyncio
    async def test_list_versions_empty_for_unknown_name(
        self, service: ToolRegistryService
    ) -> None:
        """未知 name 的版本列表为空（不抛异常）。"""
        versions = await service.list_versions("nonexistent")
        assert versions == []


# --------------------------------------------------------------------------- #
# 验收 5：ToolRegistered 事件
# --------------------------------------------------------------------------- #


class TestToolRegisteredEvent:
    """注册触发 ``ToolRegistered`` 事件（落 outbox_events）。"""

    @pytest.mark.asyncio
    async def test_register_appends_tool_registered_event(
        self, db: Database, service: ToolRegistryService
    ) -> None:
        """注册后 outbox_events 表新增一条 ``tool.registered`` 事件。"""
        actor = _actor("designer-1", roles=["DESIGNER"])
        view = await service.register_tool(EchoToolAdapter(), actor=actor)

        outbox_repo = SqliteOutboxRepository(db)
        # 取全部未发布事件
        envelopes = await outbox_repo.list_unpublished(limit=100)
        tool_events = [
            e for e in envelopes if e.event.event_type == "tool.registered"
        ]
        assert len(tool_events) == 1

        event = tool_events[0].event
        assert event.aggregate_type == "Tool"
        assert event.aggregate_id == f"echo:1.0.0"
        assert event.payload["name"] == "echo"
        assert event.payload["version"] == "1.0.0"
        assert event.payload["version_no"] == view["version_no"]
        assert event.payload["adapter_type"] == "NATIVE"
        assert event.payload["created_by"] == "designer-1"
        assert "echo" in event.payload["capabilities"]
        assert event.actor.actor_type == "USER"
        assert event.actor.actor_id == "designer-1"
        assert event.organization_id == "org-001"

    @pytest.mark.asyncio
    async def test_register_rollback_does_not_write_event(
        self, db: Database, service: ToolRegistryService
    ) -> None:
        """重复注册失败时，Outbox 事件与 tools 行均不写入（事务原子性）。"""
        actor = _actor("designer-1", roles=["DESIGNER"])
        # 第一次注册成功
        await service.register_tool(EchoToolAdapter(), actor=actor)
        # 第二次重复注册失败
        with pytest.raises(AlreadyExistsError):
            await service.register_tool(EchoToolAdapter(), actor=actor)

        outbox_repo = SqliteOutboxRepository(db)
        envelopes = await outbox_repo.list_unpublished(limit=100)
        tool_events = [
            e for e in envelopes if e.event.event_type == "tool.registered"
        ]
        # 只有第一次注册的事件，第二次失败未写
        assert len(tool_events) == 1


# --------------------------------------------------------------------------- #
# 验收 6：注销（仅 ADMIN）
# --------------------------------------------------------------------------- #


class TestUnregisterTool:
    """``unregister_tool`` 权限与正确性。"""

    @pytest.mark.asyncio
    async def test_admin_can_unregister(
        self, service: ToolRegistryService
    ) -> None:
        """ADMIN 可注销 Tool。"""
        designer = _actor("designer-1", roles=["DESIGNER"])
        admin = _actor("admin-1", roles=["ADMIN"])

        await service.register_tool(EchoToolAdapter(), actor=designer)
        result = await service.unregister_tool("echo", "1.0.0", actor=admin)
        assert result["deleted"] is True
        assert result["name"] == "echo"
        assert result["version"] == "1.0.0"

        # 注销后查询应抛 NotFoundError
        with pytest.raises(NotFoundError):
            await service.get_tool("echo", "1.0.0")

    @pytest.mark.asyncio
    async def test_designer_cannot_unregister(
        self, service: ToolRegistryService
    ) -> None:
        """DESIGNER 不能注销（仅 ADMIN）。"""
        designer = _actor("designer-1", roles=["DESIGNER"])
        await service.register_tool(EchoToolAdapter(), actor=designer)

        with pytest.raises(PermissionDeniedError) as exc_info:
            await service.unregister_tool("echo", "1.0.0", actor=designer)
        assert exc_info.value.error_code == ErrorCode.PERMISSION_DENIED
        # 错误上下文应指明是注销操作
        assert exc_info.value.context.get("action") == "unregister"

    @pytest.mark.asyncio
    async def test_observer_cannot_unregister(
        self, service: ToolRegistryService
    ) -> None:
        """OBSERVER 不能注销（无 tools:write + 非 ADMIN）。"""
        designer = _actor("designer-1", roles=["DESIGNER"])
        observer = _actor("obs-1", roles=["OBSERVER"])
        await service.register_tool(EchoToolAdapter(), actor=designer)

        with pytest.raises(PermissionDeniedError):
            await service.unregister_tool("echo", "1.0.0", actor=observer)

    @pytest.mark.asyncio
    async def test_unregister_preserves_other_versions(
        self, service: ToolRegistryService
    ) -> None:
        """注销指定版本不影响同名其他版本。"""
        designer = _actor("designer-1", roles=["DESIGNER"])
        admin = _actor("admin-1", roles=["ADMIN"])

        await service.register_tool(EchoToolAdapter(version="1.0.0"), actor=designer)
        await service.register_tool(EchoToolAdapter(version="1.1.0"), actor=designer)

        # 注销 1.0.0
        await service.unregister_tool("echo", "1.0.0", actor=admin)

        # 1.1.0 仍可查询
        v11 = await service.get_tool("echo", "1.1.0")
        assert v11["version"] == "1.1.0"

        # 1.0.0 已不存在
        with pytest.raises(NotFoundError):
            await service.get_tool("echo", "1.0.0")

        # list_versions 仅剩 1.1.0
        versions = await service.list_versions("echo")
        assert len(versions) == 1
        assert versions[0]["version"] == "1.1.0"

    @pytest.mark.asyncio
    async def test_unregister_nonexistent_raises_not_found(
        self, service: ToolRegistryService
    ) -> None:
        """注销不存在的 Tool 抛 NotFoundError。"""
        admin = _actor("admin-1", roles=["ADMIN"])
        with pytest.raises(NotFoundError):
            await service.unregister_tool("nonexistent", "1.0.0", actor=admin)


# --------------------------------------------------------------------------- #
# 验收 7：注册过程不执行 Tool
# --------------------------------------------------------------------------- #


class TestNoInvocationDuringRegister:
    """注册过程绝不调用 ``invoke`` / ``cancel``。"""

    @pytest.mark.asyncio
    async def test_invoke_not_called_during_register(
        self, service: ToolRegistryService
    ) -> None:
        """注册流程不调用 adapter.invoke。"""

        class _TrackingAdapter(EchoToolAdapter):
            invoke_count = 0
            cancel_count = 0

            async def invoke(self, definition, arguments, timeout_seconds):  # type: ignore[no-untyped-def]
                type(self).invoke_count += 1
                return await super().invoke(definition, arguments, timeout_seconds)

            async def cancel(self, external_call_id):  # type: ignore[no-untyped-def]
                type(self).cancel_count += 1
                return await super().cancel(external_call_id)

        actor = _actor("designer-1", roles=["DESIGNER"])
        adapter = _TrackingAdapter()
        await service.register_tool(adapter, actor=actor)

        assert _TrackingAdapter.invoke_count == 0
        assert _TrackingAdapter.cancel_count == 0


# --------------------------------------------------------------------------- #
# 验收 8：metadata 校验
# --------------------------------------------------------------------------- #


class TestMetadataValidation:
    """``register_tool`` 对 ``adapter.metadata`` 的校验。"""

    @pytest.mark.asyncio
    async def test_empty_name_rejected(self, service: ToolRegistryService) -> None:
        """空 name 抛 ArgumentError。"""
        actor = _actor("designer-1", roles=["DESIGNER"])

        class _BadAdapter:
            adapter_type = "NATIVE"

            @property
            def metadata(self) -> ToolMetadata:
                return ToolMetadata(name="", version="1.0.0")

            async def invoke(self, definition, arguments, timeout_seconds):  # type: ignore[no-untyped-def]
                ...

            async def cancel(self, external_call_id):  # type: ignore[no-untyped-def]
                ...

        with pytest.raises(ArgumentError):
            await service.register_tool(_BadAdapter(), actor=actor)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_empty_version_rejected(self, service: ToolRegistryService) -> None:
        """空 version 抛 ArgumentError。"""
        actor = _actor("designer-1", roles=["DESIGNER"])

        class _BadAdapter:
            adapter_type = "NATIVE"

            @property
            def metadata(self) -> ToolMetadata:
                return ToolMetadata(name="t", version="")

            async def invoke(self, definition, arguments, timeout_seconds):  # type: ignore[no-untyped-def]
                ...

            async def cancel(self, external_call_id):  # type: ignore[no-untyped-def]
                ...

        with pytest.raises(ArgumentError):
            await service.register_tool(_BadAdapter(), actor=actor)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_invalid_adapter_type_rejected(
        self, service: ToolRegistryService
    ) -> None:
        """非法 adapter_type 抛 ArgumentError。"""
        actor = _actor("designer-1", roles=["DESIGNER"])

        class _BadAdapter:
            adapter_type = "UNKNOWN"

            @property
            def metadata(self) -> ToolMetadata:
                return ToolMetadata(
                    name="t", version="1.0.0", adapter_type="UNKNOWN"
                )

            async def invoke(self, definition, arguments, timeout_seconds):  # type: ignore[no-untyped-def]
                ...

            async def cancel(self, external_call_id):  # type: ignore[no-untyped-def]
                ...

        with pytest.raises(ArgumentError):
            await service.register_tool(_BadAdapter(), actor=actor)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 验收 9：HTTP 路由契约
# --------------------------------------------------------------------------- #


class TestHttpRouter:
    """``build_tools_router`` HTTP 路由契约。"""

    def _build_app(
        self, service: ToolRegistryService, actor: ActorContext
    ) -> FastAPI:
        app = FastAPI()
        router = build_tools_router(service)
        app.include_router(router)

        # 覆盖 actor 依赖
        from maf_server.modules.tools.router import _anonymous_actor_dependency

        async def _stub_actor() -> ActorContext:
            return actor

        app.dependency_overrides[_anonymous_actor_dependency] = _stub_actor
        return app

    @pytest.mark.asyncio
    async def test_post_tool_returns_201(
        self, service: ToolRegistryService
    ) -> None:
        """POST /api/v1/tools 注册成功 201。"""
        actor = _actor("designer-1", roles=["DESIGNER"])
        app = self._build_app(service, actor)
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/tools",
                json={
                    "name": "echo",
                    "version": "1.0.0",
                    "description": "echo tool",
                    "adapter_type": "NATIVE",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "capabilities": ["echo"],
                },
            )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["name"] == "echo"
        assert data["version"] == "1.0.0"
        assert data["version_no"] == 1
        assert data["created_by"] == "designer-1"

    @pytest.mark.asyncio
    async def test_post_tool_observer_returns_403(
        self, service: ToolRegistryService
    ) -> None:
        """OBSERVER 注册返回 403。"""
        actor = _actor("obs-1", roles=["OBSERVER"])
        app = self._build_app(service, actor)
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/tools",
                json={
                    "name": "echo",
                    "version": "1.0.0",
                    "adapter_type": "NATIVE",
                },
            )
        assert resp.status_code == 403
        assert resp.json()["error"]["error_code"] == "PERMISSION_DENIED"

    @pytest.mark.asyncio
    async def test_post_tool_duplicate_returns_409(
        self, service: ToolRegistryService
    ) -> None:
        """重复注册返回 409。"""
        designer = _actor("designer-1", roles=["DESIGNER"])
        # 先注册一次
        await service.register_tool(EchoToolAdapter(), actor=designer)

        app = self._build_app(service, designer)
        with TestClient(app) as client:
            resp = client.post(
                "/api/v1/tools",
                json={
                    "name": "echo",
                    "version": "1.0.0",
                    "adapter_type": "NATIVE",
                },
            )
        assert resp.status_code == 409
        assert resp.json()["error"]["error_code"] == "ALREADY_EXISTS"

    @pytest.mark.asyncio
    async def test_get_tools_list(self, service: ToolRegistryService) -> None:
        """GET /api/v1/tools 返回列表。"""
        designer = _actor("designer-1", roles=["DESIGNER"])
        await service.register_tool(EchoToolAdapter(), actor=designer)

        # list_tools 不需要 actor
        app = FastAPI()
        app.include_router(build_tools_router(service))
        with TestClient(app) as client:
            resp = client.get("/api/v1/tools")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["name"] == "echo"

    @pytest.mark.asyncio
    async def test_get_tool_by_name_version(
        self, service: ToolRegistryService
    ) -> None:
        """GET /api/v1/tools/{name}/{version} 返回指定版本。"""
        designer = _actor("designer-1", roles=["DESIGNER"])
        await service.register_tool(EchoToolAdapter(), actor=designer)

        app = FastAPI()
        app.include_router(build_tools_router(service))
        with TestClient(app) as client:
            resp = client.get("/api/v1/tools/echo/1.0.0")
        assert resp.status_code == 200
        assert resp.json()["name"] == "echo"
        assert resp.json()["version"] == "1.0.0"

    @pytest.mark.asyncio
    async def test_get_tool_not_found_returns_404(
        self, service: ToolRegistryService
    ) -> None:
        """GET 不存在的 Tool 返回 404。"""
        app = FastAPI()
        app.include_router(build_tools_router(service))
        with TestClient(app) as client:
            resp = client.get("/api/v1/tools/nonexistent/1.0.0")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_versions(self, service: ToolRegistryService) -> None:
        """GET /api/v1/tools/{name}/versions 返回版本列表。"""
        designer = _actor("designer-1", roles=["DESIGNER"])
        await service.register_tool(EchoToolAdapter(version="1.0.0"), actor=designer)
        await service.register_tool(EchoToolAdapter(version="1.1.0"), actor=designer)

        app = FastAPI()
        app.include_router(build_tools_router(service))
        with TestClient(app) as client:
            resp = client.get("/api/v1/tools/echo/versions")
        assert resp.status_code == 200
        versions = resp.json()
        assert len(versions) == 2
        assert [v["version_no"] for v in versions] == [1, 2]

    @pytest.mark.asyncio
    async def test_delete_tool_admin_returns_200(
        self, service: ToolRegistryService
    ) -> None:
        """ADMIN DELETE 返回 200。"""
        designer = _actor("designer-1", roles=["DESIGNER"])
        admin = _actor("admin-1", roles=["ADMIN"])
        await service.register_tool(EchoToolAdapter(), actor=designer)

        app = self._build_app(service, admin)
        with TestClient(app) as client:
            resp = client.delete("/api/v1/tools/echo/1.0.0")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    @pytest.mark.asyncio
    async def test_delete_tool_designer_returns_403(
        self, service: ToolRegistryService
    ) -> None:
        """DESIGNER DELETE 返回 403。"""
        designer = _actor("designer-1", roles=["DESIGNER"])
        await service.register_tool(EchoToolAdapter(), actor=designer)

        app = self._build_app(service, designer)
        with TestClient(app) as client:
            resp = client.delete("/api/v1/tools/echo/1.0.0")
        assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# 验收 10：tools 表结构与 version_no 自增
# --------------------------------------------------------------------------- #


class TestToolsTableSchema:
    """``tools`` 表结构与 ``version_no`` 自增策略。"""

    @pytest.mark.asyncio
    async def test_tools_table_unique_constraint(
        self, db: Database, service: ToolRegistryService
    ) -> None:
        """UNIQUE(name, version) 在 DB 层强制。"""
        actor = _actor("designer-1", roles=["DESIGNER"])
        await service.register_tool(EchoToolAdapter(), actor=actor)

        # 直接 SQL 插入同名同版本应失败
        import aiosqlite

        async with db.write_connection() as conn:
            with pytest.raises(aiosqlite.IntegrityError):
                await conn.execute(
                    "INSERT INTO tools (id, name, version, description, "
                    "adapter_type, input_schema, output_schema, capabilities, "
                    "created_at, created_by, version_no) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "dup-id",
                        "echo",
                        "1.0.0",
                        "dup",
                        "NATIVE",
                        "{}",
                        "{}",
                        "[]",
                        "2025-01-01T00:00:00+00:00",
                        "designer-1",
                        99,
                    ),
                )

    @pytest.mark.asyncio
    async def test_version_no_increments_per_name(
        self, service: ToolRegistryService
    ) -> None:
        """version_no 按 name 内部自增。"""
        actor = _actor("designer-1", roles=["DESIGNER"])
        # echo v1
        v1 = await service.register_tool(EchoToolAdapter(version="1.0.0"), actor=actor)
        # other v1（不同 name，version_no 重新从 1 开始）
        v_other = await service.register_tool(
            EchoToolAdapter(name="other", version="1.0.0"), actor=actor
        )
        # echo v2
        v2 = await service.register_tool(EchoToolAdapter(version="2.0.0"), actor=actor)

        assert v1["version_no"] == 1
        assert v_other["version_no"] == 1
        assert v2["version_no"] == 2

    @pytest.mark.asyncio
    async def test_tools_table_json_valid_constraint(
        self, db: Database, service: ToolRegistryService
    ) -> None:
        """input_schema/output_schema/capabilities 列有 json_valid CHECK。"""
        actor = _actor("designer-1", roles=["DESIGNER"])
        await service.register_tool(EchoToolAdapter(), actor=actor)

        # 直接 SQL 查询验证 JSON 持久化
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT input_schema, output_schema, capabilities "
                "FROM tools WHERE name = ? AND version = ?",
                ("echo", "1.0.0"),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        input_schema = json.loads(row[0])
        output_schema = json.loads(row[1])
        capabilities = json.loads(row[2])
        assert input_schema["type"] == "object"
        assert output_schema["type"] == "object"
        assert "echo" in capabilities
