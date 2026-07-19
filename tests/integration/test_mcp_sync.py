"""TASK-049 集成测试：MCP 工具发现与同步。

验收标准覆盖：
1. ``McpClient`` 实现 ``connect`` / ``list_tools`` / ``disconnect``；
2. ``sync_mcp_tools`` 从 MCP 服务器发现工具并注册到 ToolRegistry；
3. MCP 工具注册时 ``adapter_type="MCP"``；
4. 凭据通过 ``SecretService`` 存储，不保存明文；
5. 幂等：重复同步跳过或更新（本实现采用跳过策略）；
6. 权限检查：``sync_mcp_tools`` 需 ``tools:write``（ADMIN/DESIGNER）；
7. 同步不执行远端 Tool（仅 ``list_tools``，不调用 ``tools/call``）。

测试范围：
- ``apps/server/src/maf_server/gateway/tool/mcp.py``（``McpClient`` / ``McpToolInfo``）
- ``apps/server/src/maf_server/modules/tools/{schemas,repository,service,router}.py``
  的 TASK-049 增量部分
- 不测试真实 MCP 服务器（使用 ``McpClient`` Mock 实现）
- 不测试 Tool 调用（TASK-051 范围）
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
    ArgumentError,
    ErrorCode,
    NotFoundError,
    PermissionDeniedError,
)
from maf_policy import CasbinPermissionService
from maf_server.config import ServerSettings
from maf_server.core.database import Database
from maf_server.gateway.secrets.service import SecretService
from maf_server.gateway.tool.mcp import McpClient, McpToolInfo

from maf_server.modules.tools import (
    McpServerView,
    McpToolSyncService,
    SyncError,
    SyncResult,
    ToolRegistryService,
    build_mcp_sync_router,
    init_tool_registry_schema_on_database,
)


_SECRET_PLAINTEXT = "test-secret-for-mcp-sync-task-049"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_maf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除 ``MAF_*`` 环境变量，保证测试隔离（与 test_tool_registry 一致）。"""
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
    """已初始化 tools/outbox/mcp_servers 表的 Database。"""
    settings = _make_settings(tmp_path)
    database = Database(settings)
    await database.initialize()
    await init_tool_registry_schema_on_database(database)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def registry_service(db: Database) -> ToolRegistryService:
    """ToolRegistryService，使用默认 Casbin 策略。"""
    return ToolRegistryService(db)


class InMemorySecretService:
    """内存 SecretService 桩：实现 ``SecretService`` Protocol，记录 resolve 调用。

    用于测试 ``McpToolSyncService`` 的凭据解析流程，不依赖 Keyring/AES-GCM。
    明文仅存内存，不写日志、不持久化。
    """

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}
        self._counter: int = 0
        self.resolve_calls: list[tuple[str, str, str]] = []

    async def create(
        self,
        owner_type: str,
        owner_id: str,
        plaintext: str,
        **kwargs: Any,
    ) -> str:
        self._counter += 1
        secret_id = f"sec-{self._counter:04d}"
        self._secrets[secret_id] = plaintext
        return secret_id

    async def resolve(self, secret_id: str, purpose: str, actor_id: str) -> str:
        self.resolve_calls.append((secret_id, purpose, actor_id))
        if secret_id not in self._secrets:
            raise NotFoundError(
                "secret not found",
                context={"secret_id": secret_id},
            )
        return self._secrets[secret_id]

    async def rotate(
        self, secret_id: str, new_plaintext: str, expected_version: int
    ) -> int:
        if secret_id not in self._secrets:
            raise NotFoundError("secret not found", context={"secret_id": secret_id})
        self._secrets[secret_id] = new_plaintext
        return 2

    async def delete(self, secret_id: str) -> None:
        self._secrets.pop(secret_id, None)


@pytest_asyncio.fixture
async def secret_service() -> InMemorySecretService:
    return InMemorySecretService()


def _make_tool_info(
    name: str,
    *,
    description: str = "MCP tool",
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> McpToolInfo:
    return McpToolInfo(
        name=name,
        description=description,
        input_schema=input_schema or {"type": "object", "properties": {"x": {"type": "string"}}},
        output_schema=output_schema or {"type": "object", "properties": {"result": {"type": "string"}}},
    )


def _make_mcp_client(tools: list[McpToolInfo], url: str = "http://mcp.example.com") -> McpClient:
    """构造预置工具列表的 Mock McpClient。"""
    return McpClient(tools_registry={url: tools})


@pytest_asyncio.fixture
async def sync_service(
    db: Database,
    registry_service: ToolRegistryService,
    secret_service: InMemorySecretService,
) -> McpToolSyncService:
    """McpToolSyncService，使用默认 Mock McpClient（空注册表）。"""
    return McpToolSyncService(
        db,
        registry_service,
        secret_service,  # type: ignore[arg-type]
    )


def _actor(user_id: str, roles: list[str], trace_id: str = "mcp-trace") -> ActorContext:
    """构造测试用 ActorContext。"""
    return ActorContext(
        user_id=user_id,
        organization_id="org-001",
        permission_keys=roles,
        trace_id=trace_id,
    )


_MCP_URL = "http://mcp.example.com/sse"


# --------------------------------------------------------------------------- #
# 验收 1：McpClient connect / list_tools / disconnect
# --------------------------------------------------------------------------- #


class TestMcpClient:
    """``McpClient`` Mock 实现的生命周期与发现契约。"""

    @pytest.mark.asyncio
    async def test_connect_sets_connected(self) -> None:
        """connect 后处于已连接状态，url 缓存。"""
        client = McpClient()
        assert client.connected is False
        assert client.url is None

        await client.connect(_MCP_URL)
        assert client.connected is True
        assert client.url == _MCP_URL

    @pytest.mark.asyncio
    async def test_connect_empty_url_raises(self) -> None:
        """空 url 抛 ArgumentError。"""
        client = McpClient()
        with pytest.raises(ArgumentError) as exc_info:
            await client.connect("")
        assert exc_info.value.error_code == ErrorCode.ARGUMENT_INVALID

    @pytest.mark.asyncio
    async def test_list_tools_before_connect_raises(self) -> None:
        """未连接即 list_tools 抛 RuntimeError。"""
        client = McpClient()
        with pytest.raises(RuntimeError):
            await client.list_tools()

    @pytest.mark.asyncio
    async def test_list_tools_returns_configured(self) -> None:
        """list_tools 返回预置工具列表。"""
        tools = [_make_tool_info("search"), _make_tool_info("fetch")]
        client = _make_mcp_client(tools, url=_MCP_URL)
        await client.connect(_MCP_URL)

        result = await client.list_tools()
        assert len(result) == 2
        assert result[0].name == "search"
        assert result[1].name == "fetch"

    @pytest.mark.asyncio
    async def test_list_tools_empty_for_unconfigured_url(self) -> None:
        """未配置工具的 url 返回空列表。"""
        client = McpClient()
        await client.connect("http://unknown.example.com")
        result = await client.list_tools()
        assert result == []

    @pytest.mark.asyncio
    async def test_list_tools_returns_copy(self) -> None:
        """list_tools 返回列表副本，修改不影响内部状态。"""
        tools = [_make_tool_info("t1")]
        client = _make_mcp_client(tools, url=_MCP_URL)
        await client.connect(_MCP_URL)

        result = await client.list_tools()
        result.clear()
        result2 = await client.list_tools()
        assert len(result2) == 1

    @pytest.mark.asyncio
    async def test_disconnect_clears_state(self) -> None:
        """disconnect 清空 url 与连接状态。"""
        client = _make_mcp_client([_make_tool_info("t")], url=_MCP_URL)
        await client.connect(_MCP_URL)
        assert client.connected is True

        await client.disconnect()
        assert client.connected is False
        assert client.url is None

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self) -> None:
        """disconnect 幂等，重复调用安全。"""
        client = McpClient()
        await client.connect(_MCP_URL)
        await client.disconnect()
        await client.disconnect()  # 不抛异常
        assert client.connected is False

    @pytest.mark.asyncio
    async def test_connect_credentials_not_persisted(self) -> None:
        """connect 接受 credentials 但不持久化明文（disconnect 后清空）。"""
        client = McpClient()
        await client.connect(_MCP_URL, credentials={"token": "super-secret"})
        assert client.connected is True

        await client.disconnect()
        # disconnect 后凭据引用清空，无明文残留（属性不可访问）
        assert client._credentials is None  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# 验收 2 & 3：sync_mcp_tools 注册工具 + adapter_type=MCP
# --------------------------------------------------------------------------- #


class TestSyncMcpTools:
    """``sync_mcp_tools`` 发现与注册流程。"""

    @pytest.mark.asyncio
    async def test_designer_can_sync(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """DESIGNER 可同步 MCP 工具。"""
        tools = [_make_tool_info("search"), _make_tool_info("fetch")]
        client = _make_mcp_client(tools, url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])
        result = await service.sync_mcp_tools(_MCP_URL, actor=actor)

        assert result["synced_count"] == 2
        assert result["skipped_count"] == 0
        assert result["error_count"] == 0
        assert result["server_url"] == _MCP_URL
        names = {t["name"] for t in result["synced"]}
        assert names == {"search", "fetch"}

    @pytest.mark.asyncio
    async def test_admin_can_sync(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """ADMIN 可同步 MCP 工具。"""
        client = _make_mcp_client([_make_tool_info("t")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("admin-1", ["ADMIN"])
        result = await service.sync_mcp_tools(_MCP_URL, actor=actor)
        assert result["synced_count"] == 1

    @pytest.mark.asyncio
    async def test_observer_cannot_sync(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """OBSERVER 不能同步（无 tools:write）。"""
        client = _make_mcp_client([_make_tool_info("t")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("obs-1", ["OBSERVER"])
        with pytest.raises(PermissionDeniedError) as exc_info:
            await service.sync_mcp_tools(_MCP_URL, actor=actor)
        assert exc_info.value.error_code == ErrorCode.PERMISSION_DENIED

    @pytest.mark.asyncio
    async def test_sync_registers_adapter_type_mcp(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """同步注册的 Tool adapter_type 必须为 MCP。"""
        client = _make_mcp_client([_make_tool_info("search")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])
        result = await service.sync_mcp_tools(_MCP_URL, actor=actor)

        assert result["synced_count"] == 1
        tool = result["synced"][0]
        assert tool["adapter_type"] == "MCP"
        assert tool["name"] == "search"
        assert tool["version"] == "1.0.0"
        assert "mcp" in tool["capabilities"]
        assert tool["input_schema"]["type"] == "object"

        # 验证持久化到 tools 表
        view = await registry_service.get_tool("search", "1.0.0")
        assert view["adapter_type"] == "MCP"

    @pytest.mark.asyncio
    async def test_sync_empty_url_raises(
        self, sync_service: McpToolSyncService
    ) -> None:
        """空 server_url 抛 ArgumentError。"""
        actor = _actor("designer-1", ["DESIGNER"])
        with pytest.raises(ArgumentError):
            await sync_service.sync_mcp_tools("", actor=actor)

    @pytest.mark.asyncio
    async def test_sync_with_empty_tool_list(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """MCP 服务器无工具时返回空结果，但仍记录服务器配置。"""
        client = _make_mcp_client([], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])
        result = await service.sync_mcp_tools(_MCP_URL, actor=actor)
        assert result["synced_count"] == 0
        assert result["error_count"] == 0

        # 服务器配置仍写入
        servers = await service.list_mcp_servers(actor=actor)
        assert len(servers) == 1
        assert servers[0]["url"] == _MCP_URL

    @pytest.mark.asyncio
    async def test_sync_invalid_empty_tool_name_records_error(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """工具 name 为空时记入 errors，不中断整体同步。"""
        bad_tool = McpToolInfo(name="", description="bad")
        good_tool = _make_tool_info("good")
        client = McpClient(tools_registry={_MCP_URL: [bad_tool, good_tool]})
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])
        result = await service.sync_mcp_tools(_MCP_URL, actor=actor)

        assert result["synced_count"] == 1
        assert result["error_count"] == 1
        assert result["errors"][0]["code"] == "INVALID_SCHEMA"
        assert result["synced"][0]["name"] == "good"

    @pytest.mark.asyncio
    async def test_sync_connect_failure_returns_error(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """MCP 连接失败时返回 CONNECT_FAILED 错误，不注册任何工具。"""

        class _FailingClient(McpClient):
            async def connect(self, url, *, credentials=None):  # type: ignore[no-untyped-def]
                raise RuntimeError("connection refused")

        client = _FailingClient()
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])
        result = await service.sync_mcp_tools(_MCP_URL, actor=actor)

        assert result["synced_count"] == 0
        assert result["error_count"] == 1
        assert result["errors"][0]["code"] == "CONNECT_FAILED"
        assert "connection refused" in result["errors"][0]["message"]
        assert result["errors"][0]["tool_name"] == "<server>"


# --------------------------------------------------------------------------- #
# 验收 4：凭据安全（SecretService）
# --------------------------------------------------------------------------- #


class TestCredentialSecurity:
    """凭据经 ``SecretService`` 存储，不保存明文。"""

    @pytest.mark.asyncio
    async def test_sync_resolves_credential_via_secret_service(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """sync_mcp_tools 通过 SecretService.resolve 解析凭据。"""
        secret_id = await secret_service.create(
            "mcp_server", _MCP_URL, "mcp-token-plaintext"
        )
        client = _make_mcp_client([_make_tool_info("t")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])
        await service.sync_mcp_tools(
            _MCP_URL, credential_secret_id=secret_id, actor=actor
        )

        # 验证 resolve 被调用，purpose 为 mcp.call
        assert len(secret_service.resolve_calls) == 1
        called_secret_id, purpose, actor_id = secret_service.resolve_calls[0]
        assert called_secret_id == secret_id
        assert purpose == "mcp.call"
        assert actor_id == "designer-1"

    @pytest.mark.asyncio
    async def test_credential_secret_id_stored_not_plaintext(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """mcp_servers 表存 credential_secret_id 引用，不存明文。"""
        plaintext = "super-secret-token-12345"
        secret_id = await secret_service.create("mcp_server", _MCP_URL, plaintext)
        client = _make_mcp_client([_make_tool_info("t")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])
        await service.sync_mcp_tools(
            _MCP_URL, credential_secret_id=secret_id, actor=actor
        )

        # 直接查 mcp_servers 表
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT url, name, credential_secret_id FROM mcp_servers WHERE url = ?",
                (_MCP_URL,),
            ) as cur:
                row = await cur.fetchone()

        assert row is not None
        assert row[0] == _MCP_URL
        # credential_secret_id 列存的是引用 ID，不是明文
        assert row[2] == secret_id
        assert plaintext not in str(row)

    @pytest.mark.asyncio
    async def test_credential_plaintext_not_in_database(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """凭据明文不出现在任何数据库表中（mcp_servers / tools / outbox_events）。"""
        plaintext = "DO_NOT_LEAK_THIS_TOKEN_xyz"
        secret_id = await secret_service.create("mcp_server", _MCP_URL, plaintext)
        client = _make_mcp_client([_make_tool_info("leak-check")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])
        await service.sync_mcp_tools(
            _MCP_URL, credential_secret_id=secret_id, actor=actor
        )

        # 扫描所有业务表，明文不应出现
        async with db.read_connection() as conn:
            for table in ("mcp_servers", "tools", "outbox_events"):
                async with conn.execute(f"SELECT * FROM {table}") as cur:
                    rows = await cur.fetchall()
                for row in rows:
                    row_text = json.dumps(
                        [str(c) if c is not None else "" for c in row],
                        ensure_ascii=False,
                    )
                    assert plaintext not in row_text, (
                        f"凭据明文泄漏到 {table} 表: {row_text}"
                    )

    @pytest.mark.asyncio
    async def test_sync_resync_uses_stored_secret_id(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """重同步不传 credential_secret_id 时回退已存引用。"""
        plaintext = "stored-token-for-resync"
        secret_id = await secret_service.create("mcp_server", _MCP_URL, plaintext)
        client = _make_mcp_client([_make_tool_info("t")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])
        # 首次同步传入 secret_id
        await service.sync_mcp_tools(
            _MCP_URL, credential_secret_id=secret_id, actor=actor
        )
        assert len(secret_service.resolve_calls) == 1

        # 重同步不传 secret_id，应回退已存引用并 resolve
        await service.sync_mcp_tools(_MCP_URL, actor=actor)
        assert len(secret_service.resolve_calls) == 2
        # 第二次 resolve 用的是同一个 secret_id
        assert secret_service.resolve_calls[1][0] == secret_id

    @pytest.mark.asyncio
    async def test_sync_without_credential_works(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """无凭据的 MCP 服务器也能同步（公开服务器）。"""
        client = _make_mcp_client([_make_tool_info("public-tool")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])
        result = await service.sync_mcp_tools(_MCP_URL, actor=actor)

        assert result["synced_count"] == 1
        assert len(secret_service.resolve_calls) == 0

        # mcp_servers 配置 credential_secret_id 为 None
        servers = await service.list_mcp_servers(actor=actor)
        assert servers[0]["credential_secret_id"] is None

    @pytest.mark.asyncio
    async def test_sync_secret_resolve_failure_returns_error(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """凭据解析失败时返回 SECRET_RESOLVE_FAILED，不注册工具。"""
        client = _make_mcp_client([_make_tool_info("t")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])
        result = await service.sync_mcp_tools(
            _MCP_URL, credential_secret_id="nonexistent-secret", actor=actor
        )

        assert result["synced_count"] == 0
        assert result["error_count"] == 1
        assert result["errors"][0]["code"] == "SECRET_RESOLVE_FAILED"


# --------------------------------------------------------------------------- #
# 验收 5：幂等同步
# --------------------------------------------------------------------------- #


class TestIdempotentSync:
    """重复同步相同工具时跳过（幂等）。"""

    @pytest.mark.asyncio
    async def test_resync_skips_already_registered(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """重复同步相同工具：第二次全部记入 skipped，不重复注册。"""
        tools = [_make_tool_info("search"), _make_tool_info("fetch")]
        client = _make_mcp_client(tools, url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])

        # 首次同步：全部注册
        result1 = await service.sync_mcp_tools(_MCP_URL, actor=actor)
        assert result1["synced_count"] == 2
        assert result1["skipped_count"] == 0

        # 二次同步：全部跳过（幂等）
        result2 = await service.sync_mcp_tools(_MCP_URL, actor=actor)
        assert result2["synced_count"] == 0
        assert result2["skipped_count"] == 2
        assert set(result2["skipped"]) == {"search", "fetch"}
        assert result2["error_count"] == 0

        # tools 表中仍然只有 2 条（无重复）
        listing = await registry_service.list_tools()
        assert len(listing["items"]) == 2

    @pytest.mark.asyncio
    async def test_resync_version_no_increments_on_server(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """重同步时 mcp_servers.version_no 递增（记录同步次数）。"""
        client = _make_mcp_client([_make_tool_info("t")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])
        await service.sync_mcp_tools(_MCP_URL, actor=actor)
        await service.sync_mcp_tools(_MCP_URL, actor=actor)

        servers = await service.list_mcp_servers(actor=actor)
        assert len(servers) == 1
        assert servers[0]["version_no"] == 2

    @pytest.mark.asyncio
    async def test_partial_idempotent_sync(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """部分已注册工具：新工具注册，已注册工具跳过。"""
        # 首次：1 个工具
        client1 = _make_mcp_client([_make_tool_info("alpha")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client1)  # type: ignore[arg-type]
        actor = _actor("designer-1", ["DESIGNER"])
        await service.sync_mcp_tools(_MCP_URL, actor=actor)

        # 二次：2 个工具（alpha 已注册，beta 新增）
        client2 = _make_mcp_client(
            [_make_tool_info("alpha"), _make_tool_info("beta")], url=_MCP_URL
        )
        service2 = McpToolSyncService(db, registry_service, secret_service, mcp_client=client2)  # type: ignore[arg-type]
        result = await service2.sync_mcp_tools(_MCP_URL, actor=actor)

        assert result["synced_count"] == 1
        assert result["skipped_count"] == 1
        assert result["synced"][0]["name"] == "beta"
        assert result["skipped"] == ["alpha"]


# --------------------------------------------------------------------------- #
# 验收 7：同步不执行远端 Tool
# --------------------------------------------------------------------------- #


class TestNoToolExecutionDuringSync:
    """同步流程绝不调用 ``tools/call``（仅 list_tools 发现）。"""

    @pytest.mark.asyncio
    async def test_sync_only_calls_connect_list_disconnect(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """sync 仅调用 connect/list_tools/disconnect，不调用 call_tool。"""

        class _TrackingClient(McpClient):
            def __init__(self, tools_registry=None):
                super().__init__(tools_registry=tools_registry)
                self.calls: list[str] = []

            async def connect(self, url, *, credentials=None):  # type: ignore[no-untyped-def]
                self.calls.append("connect")
                await super().connect(url, credentials=credentials)

            async def list_tools(self):  # type: ignore[no-untyped-def]
                self.calls.append("list_tools")
                return await super().list_tools()

            async def disconnect(self):  # type: ignore[no-untyped-def]
                self.calls.append("disconnect")
                await super().disconnect()

        client = _TrackingClient(tools_registry={_MCP_URL: [_make_tool_info("t")]})
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])
        await service.sync_mcp_tools(_MCP_URL, actor=actor)

        # 仅 connect / list_tools / disconnect，无 call_tool
        assert client.calls == ["connect", "list_tools", "disconnect"]
        assert "call_tool" not in client.calls


# --------------------------------------------------------------------------- #
# mcp_servers 配置表与 list/remove
# --------------------------------------------------------------------------- #


class TestMcpServerConfig:
    """``mcp_servers`` 表与 ``list_mcp_servers`` / ``remove_mcp_server``。"""

    @pytest.mark.asyncio
    async def test_sync_upserts_server_config(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """同步后 mcp_servers 表记录服务器配置。"""
        client = _make_mcp_client([_make_tool_info("t")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])
        await service.sync_mcp_tools(_MCP_URL, actor=actor)

        servers = await service.list_mcp_servers(actor=actor)
        assert len(servers) == 1
        server = servers[0]
        assert server["url"] == _MCP_URL
        assert server["name"] == "mcp.example.com"  # 从 url host 推导
        assert server["synced_by"] == "designer-1"
        assert server["version_no"] == 1
        assert server["last_synced_at"] is not None

    @pytest.mark.asyncio
    async def test_list_mcp_servers_empty(
        self, sync_service: McpToolSyncService
    ) -> None:
        """无配置时 list_mcp_servers 返回空列表。"""
        actor = _actor("obs-1", ["OBSERVER"])
        servers = await sync_service.list_mcp_servers(actor=actor)
        assert servers == []

    @pytest.mark.asyncio
    async def test_observer_can_list_servers(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """OBSERVER 有 tools:read，可列出 MCP 服务器。"""
        client = _make_mcp_client([_make_tool_info("t")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        designer = _actor("designer-1", ["DESIGNER"])
        await service.sync_mcp_tools(_MCP_URL, actor=designer)

        observer = _actor("obs-1", ["OBSERVER"])
        servers = await service.list_mcp_servers(actor=observer)
        assert len(servers) == 1

    @pytest.mark.asyncio
    async def test_unauthenticated_actor_cannot_list(
        self, sync_service: McpToolSyncService
    ) -> None:
        """无 permission_keys 的 actor 不能列出。"""
        bad_actor = ActorContext(
            user_id="",
            organization_id="org-001",
            permission_keys=[],
            trace_id="t",
        )
        with pytest.raises(PermissionDeniedError):
            await sync_service.list_mcp_servers(actor=bad_actor)

    @pytest.mark.asyncio
    async def test_remove_mcp_server(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """DESIGNER 可移除 MCP 服务器配置。"""
        client = _make_mcp_client([_make_tool_info("t")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        designer = _actor("designer-1", ["DESIGNER"])
        await service.sync_mcp_tools(_MCP_URL, actor=designer)

        await service.remove_mcp_server(_MCP_URL, actor=designer)

        servers = await service.list_mcp_servers(actor=designer)
        assert servers == []

    @pytest.mark.asyncio
    async def test_observer_cannot_remove(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """OBSERVER 不能移除（无 tools:write）。"""
        client = _make_mcp_client([_make_tool_info("t")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        designer = _actor("designer-1", ["DESIGNER"])
        await service.sync_mcp_tools(_MCP_URL, actor=designer)

        observer = _actor("obs-1", ["OBSERVER"])
        with pytest.raises(PermissionDeniedError):
            await service.remove_mcp_server(_MCP_URL, actor=observer)

    @pytest.mark.asyncio
    async def test_remove_nonexistent_raises_not_found(
        self, sync_service: McpToolSyncService
    ) -> None:
        """移除不存在的服务器抛 NotFoundError。"""
        actor = _actor("designer-1", ["DESIGNER"])
        with pytest.raises(NotFoundError) as exc_info:
            await sync_service.remove_mcp_server("http://nope.example.com", actor=actor)
        assert exc_info.value.error_code == ErrorCode.NOT_FOUND

    @pytest.mark.asyncio
    async def test_remove_preserves_registered_tools(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """移除服务器配置不影响已注册工具（不删除历史）。"""
        client = _make_mcp_client([_make_tool_info("search")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        designer = _actor("designer-1", ["DESIGNER"])
        await service.sync_mcp_tools(_MCP_URL, actor=designer)

        # 移除服务器配置
        await service.remove_mcp_server(_MCP_URL, actor=designer)

        # 工具仍可查询
        view = await registry_service.get_tool("search", "1.0.0")
        assert view["adapter_type"] == "MCP"

        listing = await registry_service.list_tools()
        assert len(listing["items"]) == 1

    @pytest.mark.asyncio
    async def test_multiple_servers_tracked_separately(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """多个 MCP 服务器分别记录配置。"""
        url_a = "http://mcp-a.example.com/sse"
        url_b = "http://mcp-b.example.com/sse"
        client = McpClient(
            tools_registry={
                url_a: [_make_tool_info("tool_a")],
                url_b: [_make_tool_info("tool_b")],
            }
        )
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])
        await service.sync_mcp_tools(url_a, actor=actor)
        await service.sync_mcp_tools(url_b, actor=actor)

        servers = await service.list_mcp_servers(actor=actor)
        assert len(servers) == 2
        urls = {s["url"] for s in servers}
        assert urls == {url_a, url_b}


# --------------------------------------------------------------------------- #
# 验收：mcp_servers 表结构
# --------------------------------------------------------------------------- #


class TestMcpServersTableSchema:
    """``mcp_servers`` 表结构约束。"""

    @pytest.mark.asyncio
    async def test_mcp_servers_url_primary_key(
        self, db: Database, registry_service: ToolRegistryService, secret_service: InMemorySecretService
    ) -> None:
        """url 是主键，重复 upsert 更新而非插入。"""
        import aiosqlite

        client = _make_mcp_client([_make_tool_info("t")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])
        await service.sync_mcp_tools(_MCP_URL, actor=actor)
        await service.sync_mcp_tools(_MCP_URL, actor=actor)

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM mcp_servers WHERE url = ?", (_MCP_URL,)
            ) as cur:
                row = await cur.fetchone()
        assert row[0] == 1  # 仅一行（upsert 更新）

    @pytest.mark.asyncio
    async def test_mcp_servers_credential_secret_id_nullable(
        self, db: Database
    ) -> None:
        """credential_secret_id 列允许 NULL（无凭据服务器）。"""
        async with db.write_connection() as conn:
            await conn.execute(
                "INSERT INTO mcp_servers (url, name, credential_secret_id, "
                "last_synced_at, synced_by, version_no) VALUES (?, ?, NULL, NULL, '', 1)",
                ("http://public.example.com", "public"),
            )
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT credential_secret_id FROM mcp_servers WHERE url = ?",
                ("http://public.example.com",),
            ) as cur:
                row = await cur.fetchone()
        assert row[0] is None


# --------------------------------------------------------------------------- #
# HTTP 路由契约
# --------------------------------------------------------------------------- #


class TestMcpSyncHttpRouter:
    """``build_mcp_sync_router`` HTTP 路由契约。"""

    def _build_app(
        self,
        service: McpToolSyncService,
        actor: ActorContext,
    ) -> FastAPI:
        app = FastAPI()
        app.include_router(build_mcp_sync_router(service))

        from maf_server.modules.tools.router import _anonymous_actor_dependency

        async def _stub_actor() -> ActorContext:
            return actor

        app.dependency_overrides[_anonymous_actor_dependency] = _stub_actor
        return app

    @pytest.mark.asyncio
    async def test_post_sync_returns_200(
        self,
        db: Database,
        registry_service: ToolRegistryService,
        secret_service: InMemorySecretService,
    ) -> None:
        """POST /api/v1/mcp-servers/sync 同步成功 200。"""
        client = _make_mcp_client([_make_tool_info("search")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("designer-1", ["DESIGNER"])
        app = self._build_app(service, actor)
        with TestClient(app) as http_client:
            resp = http_client.post(
                "/api/v1/mcp-servers/sync",
                json={"server_url": _MCP_URL},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["synced_count"] == 1
        assert data["synced"][0]["adapter_type"] == "MCP"
        assert data["server_url"] == _MCP_URL

    @pytest.mark.asyncio
    async def test_post_sync_observer_returns_403(
        self,
        db: Database,
        registry_service: ToolRegistryService,
        secret_service: InMemorySecretService,
    ) -> None:
        """OBSERVER 同步返回 403。"""
        client = _make_mcp_client([_make_tool_info("t")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        actor = _actor("obs-1", ["OBSERVER"])
        app = self._build_app(service, actor)
        with TestClient(app) as http_client:
            resp = http_client.post(
                "/api/v1/mcp-servers/sync",
                json={"server_url": _MCP_URL},
            )
        assert resp.status_code == 403
        assert resp.json()["error"]["error_code"] == "PERMISSION_DENIED"

    @pytest.mark.asyncio
    async def test_get_mcp_servers_list(
        self,
        db: Database,
        registry_service: ToolRegistryService,
        secret_service: InMemorySecretService,
    ) -> None:
        """GET /api/v1/mcp-servers 返回服务器列表。"""
        client = _make_mcp_client([_make_tool_info("t")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        designer = _actor("designer-1", ["DESIGNER"])
        await service.sync_mcp_tools(_MCP_URL, actor=designer)

        observer = _actor("obs-1", ["OBSERVER"])
        app = self._build_app(service, observer)
        with TestClient(app) as http_client:
            resp = http_client.get("/api/v1/mcp-servers")
        assert resp.status_code == 200
        servers = resp.json()
        assert len(servers) == 1
        assert servers[0]["url"] == _MCP_URL

    @pytest.mark.asyncio
    async def test_delete_mcp_server_returns_204(
        self,
        db: Database,
        registry_service: ToolRegistryService,
        secret_service: InMemorySecretService,
    ) -> None:
        """DELETE /api/v1/mcp-servers 移除成功 204。"""
        client = _make_mcp_client([_make_tool_info("t")], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        designer = _actor("designer-1", ["DESIGNER"])
        await service.sync_mcp_tools(_MCP_URL, actor=designer)

        app = self._build_app(service, designer)
        with TestClient(app) as http_client:
            resp = http_client.request(
                "DELETE",
                "/api/v1/mcp-servers",
                json={"server_url": _MCP_URL},
            )
        assert resp.status_code == 204

        # 验证已移除
        servers = await service.list_mcp_servers(actor=designer)
        assert servers == []

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(
        self,
        db: Database,
        registry_service: ToolRegistryService,
        secret_service: InMemorySecretService,
    ) -> None:
        """DELETE 不存在的服务器返回 404。"""
        client = _make_mcp_client([], url=_MCP_URL)
        service = McpToolSyncService(db, registry_service, secret_service, mcp_client=client)  # type: ignore[arg-type]

        designer = _actor("designer-1", ["DESIGNER"])
        app = self._build_app(service, designer)
        with TestClient(app) as http_client:
            resp = http_client.request(
                "DELETE",
                "/api/v1/mcp-servers",
                json={"server_url": "http://nope.example.com"},
            )
        assert resp.status_code == 404
