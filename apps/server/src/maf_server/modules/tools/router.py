"""Tool 配置公共 HTTP 接口。

TASK-048 扩展：
- 保留原有 ``ToolConfigurationHttpApi`` Protocol（其他任务接口契约，
  TASK-049/050 范围）；
- 新增 ``build_tools_router`` 工厂，构造 FastAPI ``APIRouter``，挂载
  Tool Registry REST API：
    - ``POST /api/v1/tools``：注册（DESIGNER/ADMIN）；
    - ``GET /api/v1/tools``：列出全部；
    - ``GET /api/v1/tools/{name}/{version}``：获取指定版本；
    - ``DELETE /api/v1/tools/{name}/{version}``：注销（仅 ADMIN）；
    - ``GET /api/v1/tools/{name}/versions``：版本列表。

路由前缀使用 ``/api/v1/tools``，与现有 IAM 路由 ``/api/v1/auth`` 对齐。
本任务范围只声明路由；正式认证中间件由后续任务落地，开发期通过
``app.dependency_overrides`` 注入 stub actor。
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing_extensions import Annotated

from maf_contracts.common import ActorContext
from maf_domain.errors import (
    AlreadyExistsError,
    DomainError,
    NotFoundError,
    PermissionDeniedError,
    UnauthenticatedError,
)

from .schemas import (
    CapabilityDecisionView,
    PolicySimulationRequest,
    RegisterToolRequest,
    SyncMcpToolsRequest,
    SyncMcpToolsResult,
    ToolListResult,
    ToolRegistrationView,
    ToolVersionView,
    ToolView,
    UnregisterToolResult,
)


# --------------------------------------------------------------------------- #
# 原有 Protocol（保留 TASK-049/050 接口契约）
# --------------------------------------------------------------------------- #


class ToolConfigurationHttpApi(Protocol):
    async def post_tool(self, actor: ActorContext, request: RegisterToolRequest) -> ToolView:
        """POST `/api/v1/tools`；注册成功 201。"""
        ...

    async def post_sync_mcp(self, actor: ActorContext, mcp_server_id: str, request: SyncMcpToolsRequest) -> SyncMcpToolsResult:
        """POST `/api/v1/mcp-servers/{id}/sync-tools`；同步完成 200。"""
        ...

    async def post_policy_simulation(self, actor: ActorContext, request: PolicySimulationRequest) -> CapabilityDecisionView:
        """POST `/api/v1/policies/simulate`；只返回决策，不产生外部动作。"""
        ...


# --------------------------------------------------------------------------- #
# Pydantic 请求/响应模型
# --------------------------------------------------------------------------- #


class ToolMetadataPayload(BaseModel):
    """Adapter.metadata 的 HTTP 投影。

    客户端通过此 payload 描述待注册 Tool 的元数据；服务端构造一个一次性的
    ``_StaticMetadataAdapter`` 包装该 metadata，调用 ``register_tool``。
    本任务不实际执行 Tool，``invoke`` 不在注册路径上被调用。
    """

    name: str = Field(..., min_length=1, max_length=128, description="Tool 业务名")
    version: str = Field(..., min_length=1, max_length=64, description="语义版本字符串")
    description: str = Field("", max_length=2048, description="Tool 描述")
    adapter_type: Literal["NATIVE", "HTTP", "MCP"] = Field(
        "NATIVE", description="Adapter 类型"
    )
    input_schema: dict[str, Any] = Field(
        default_factory=dict, description="输入 JSON Schema"
    )
    output_schema: dict[str, Any] = Field(
        default_factory=dict, description="输出 JSON Schema"
    )
    capabilities: list[str] = Field(
        default_factory=list, description="能力标识列表"
    )


class ToolRegistrationOut(BaseModel):
    """Tool 注册视图响应体。"""

    id: str
    name: str
    version: str
    description: str
    adapter_type: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    capabilities: list[str]
    version_no: int
    created_at: str
    created_by: str


class ToolVersionOut(BaseModel):
    """Tool 版本列表条目响应体。"""

    version: str
    version_no: int
    description: str
    adapter_type: str
    created_at: str
    created_by: str


class ToolListOut(BaseModel):
    """Tool 列表响应体。"""

    items: list[ToolRegistrationOut]


class UnregisterToolOut(BaseModel):
    """Tool 注销响应体。"""

    name: str
    version: str
    deleted: bool


class ErrorResponse(BaseModel):
    """对外错误响应体。"""

    error_code: str
    message: str
    retryable: bool = False


# --------------------------------------------------------------------------- #
# ActorContext 依赖（占位；正式认证中间件在后续任务落地）
# --------------------------------------------------------------------------- #


async def _anonymous_actor_dependency() -> ActorContext:
    """占位 actor 依赖；正式认证中间件落地前由 ``app.dependency_overrides`` 覆盖。

    本函数永远抛 ``UnauthenticatedError``；测试通过依赖注入覆盖。
    """
    raise UnauthenticatedError("未认证")


ActorDep = Annotated[ActorContext, Depends(_anonymous_actor_dependency)]


# --------------------------------------------------------------------------- #
# 内部：把 HTTP payload 包装成 ToolAdapter
# --------------------------------------------------------------------------- #


class _StaticMetadataAdapter:
    """把 HTTP 请求体携带的 metadata 包装成 ``ToolAdapter`` 鸭子类型。

    注册流程只读取 ``adapter.metadata``，不调用 ``invoke`` / ``cancel``；
    本类提供这两个方法的桩实现以满足 Protocol 结构，但不会被注册流程触发。
    """

    def __init__(self, metadata_payload: ToolMetadataPayload) -> None:
        from maf_tool_adapters import ToolMetadata

        self.adapter_type: str = metadata_payload.adapter_type
        self._metadata: ToolMetadata = ToolMetadata(
            name=metadata_payload.name,
            version=metadata_payload.version,
            description=metadata_payload.description,
            input_schema=metadata_payload.input_schema,
            output_schema=metadata_payload.output_schema,
            capabilities=list(metadata_payload.capabilities),
            adapter_type=metadata_payload.adapter_type,
        )

    @property
    def metadata(self):  # type: ignore[no-untyped-def]
        return self._metadata

    async def invoke(
        self,
        definition: dict[str, Any],
        arguments: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """注册流程不会调用本方法；仅满足 Protocol 结构。"""
        raise NotImplementedError("ToolRegistry HTTP 路由不调用 invoke")

    async def cancel(self, external_call_id: str) -> None:
        """注册流程不会调用本方法；仅满足 Protocol 结构。"""
        raise NotImplementedError("ToolRegistry HTTP 路由不调用 cancel")


# --------------------------------------------------------------------------- #
# 错误映射
# --------------------------------------------------------------------------- #


def _error_response(exc: DomainError) -> JSONResponse:
    """把 ``DomainError`` 映射为 HTTP JSON 响应。"""
    code = exc.error_code.value
    if code == "PERMISSION_DENIED":
        http_status = 403
    elif code == "UNAUTHENTICATED":
        http_status = 401
    elif code == "NOT_FOUND":
        http_status = 404
    elif code in ("ALREADY_EXISTS", "VERSION_CONFLICT", "IDEMPOTENCY_CONFLICT"):
        http_status = 409
    elif code in ("ARGUMENT_INVALID", "SCHEMA_VALIDATION_FAILED", "UNSUPPORTED_OPERATION"):
        http_status = 422
    else:
        http_status = 500
    return JSONResponse(
        status_code=http_status,
        content={
            "error": {
                "error_code": code,
                "message": exc.message,
                "retryable": exc.retryable,
            }
        },
    )


# --------------------------------------------------------------------------- #
# 用于类型注解的鸭子类型协议
# --------------------------------------------------------------------------- #


class ToolRegistryServiceLike(Protocol):
    """``build_tools_router`` 接受的 service 鸭子类型。"""

    async def register_tool(
        self, adapter: Any, *, actor: ActorContext
    ) -> ToolRegistrationView: ...

    async def list_tools(self) -> ToolListResult: ...

    async def get_tool(self, name: str, version: str) -> ToolRegistrationView: ...

    async def list_versions(self, name: str) -> list[ToolVersionView]: ...

    async def unregister_tool(
        self, name: str, version: str, *, actor: ActorContext
    ) -> UnregisterToolResult: ...


# --------------------------------------------------------------------------- #
# FastAPI Router 工厂
# --------------------------------------------------------------------------- #


def build_tools_router(service: ToolRegistryServiceLike) -> APIRouter:
    """构造 Tool Registry FastAPI ``APIRouter``。

    路由：
        - ``POST   /api/v1/tools``：注册；
        - ``GET    /api/v1/tools``：列出全部；
        - ``GET    /api/v1/tools/{name}/versions``：版本列表（必须在
          ``/{name}/{version}`` 之前注册，避免 ``versions`` 被当作 version）；
        - ``GET    /api/v1/tools/{name}/{version}``：获取指定版本；
        - ``DELETE /api/v1/tools/{name}/{version}``：注销。

    :param service: ``ToolRegistryService`` 或鸭子类型对象。
    :returns: FastAPI ``APIRouter``，由调用方挂载到 app。
    """
    router = APIRouter(prefix="/api/v1/tools", tags=["tools"])

    @router.post(
        "",
        response_model=ToolRegistrationOut,
        status_code=status.HTTP_201_CREATED,
        responses={
            401: {"model": ErrorResponse, "description": "未认证"},
            403: {"model": ErrorResponse, "description": "权限不足"},
            409: {"model": ErrorResponse, "description": "Tool 已注册"},
            422: {"model": ErrorResponse, "description": "metadata 校验失败"},
        },
    )
    async def register_tool(  # noqa: ANN202
        payload: ToolMetadataPayload,
        actor: ActorDep,
    ) -> ToolRegistrationOut | JSONResponse:
        """注册 Tool 元数据；不执行 Tool。"""
        adapter = _StaticMetadataAdapter(payload)
        try:
            view = await service.register_tool(adapter, actor=actor)
        except DomainError as exc:
            return _error_response(exc)
        return ToolRegistrationOut(**view)

    @router.get(
        "",
        response_model=ToolListOut,
        status_code=status.HTTP_200_OK,
    )
    async def list_tools() -> ToolListOut:  # noqa: ANN202
        """列出全部已注册 Tool。"""
        result = await service.list_tools()
        return ToolListOut(
            items=[ToolRegistrationOut(**item) for item in result["items"]]
        )

    @router.get(
        "/{name}/versions",
        response_model=list[ToolVersionOut],
        status_code=status.HTTP_200_OK,
    )
    async def list_versions(
        name: str = Path(..., min_length=1, max_length=128),
    ) -> list[ToolVersionOut]:  # noqa: ANN202
        """列出指定 Tool 的全部版本。"""
        versions = await service.list_versions(name)
        return [ToolVersionOut(**v) for v in versions]

    @router.get(
        "/{name}/{version}",
        response_model=ToolRegistrationOut,
        status_code=status.HTTP_200_OK,
        responses={
            404: {"model": ErrorResponse, "description": "Tool 不存在"},
        },
    )
    async def get_tool(
        name: str = Path(..., min_length=1, max_length=128),
        version: str = Path(..., min_length=1, max_length=64),
    ) -> ToolRegistrationOut | JSONResponse:  # noqa: ANN202
        """获取指定版本的 Tool。"""
        try:
            view = await service.get_tool(name, version)
        except DomainError as exc:
            return _error_response(exc)
        return ToolRegistrationOut(**view)

    @router.delete(
        "/{name}/{version}",
        response_model=UnregisterToolOut,
        status_code=status.HTTP_200_OK,
        responses={
            401: {"model": ErrorResponse, "description": "未认证"},
            403: {"model": ErrorResponse, "description": "权限不足"},
            404: {"model": ErrorResponse, "description": "Tool 不存在"},
        },
    )
    async def unregister_tool(
        actor: ActorDep,
        name: str = Path(..., min_length=1, max_length=128),
        version: str = Path(..., min_length=1, max_length=64),
    ) -> UnregisterToolOut | JSONResponse:  # noqa: ANN202
        """注销指定版本的 Tool；仅 ADMIN 可调用。"""
        try:
            result = await service.unregister_tool(name, version, actor=actor)
        except DomainError as exc:
            return _error_response(exc)
        return UnregisterToolOut(**result)

    return router


# --------------------------------------------------------------------------- #
# TASK-049: MCP 工具同步 REST API
# --------------------------------------------------------------------------- #


class SyncMcpToolsPayload(BaseModel):
    """``POST /api/v1/mcp-servers/sync`` 请求体。"""

    server_url: str = Field(..., min_length=1, description="MCP 服务器 endpoint")
    credential_secret_id: str | None = Field(
        None, description="凭据 SecretService 引用 ID（无明文）"
    )


class SyncErrorOut(BaseModel):
    """单条同步错误响应体。"""

    tool_name: str
    code: str
    message: str


class SyncedToolOut(BaseModel):
    """同步注册成功的 Tool 视图响应体。"""

    id: str
    name: str
    version: str
    description: str
    adapter_type: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    capabilities: list[str]
    version_no: int
    created_at: str
    created_by: str


class SyncMcpToolsResultOut(BaseModel):
    """``POST /api/v1/mcp-servers/sync`` 响应体。"""

    server_url: str
    synced: list[SyncedToolOut]
    skipped: list[str]
    errors: list[SyncErrorOut]
    synced_count: int
    skipped_count: int
    error_count: int


class McpServerOut(BaseModel):
    """MCP 服务器视图响应体。"""

    url: str
    name: str
    credential_secret_id: str | None
    last_synced_at: str | None
    synced_by: str
    version_no: int


class RemoveMcpServerPayload(BaseModel):
    """``DELETE /api/v1/mcp-servers`` 请求体。"""

    server_url: str = Field(..., min_length=1, description="MCP 服务器 endpoint")


class McpToolSyncServiceLike(Protocol):
    """``build_mcp_sync_router`` 接受的 service 鸭子类型。"""

    async def sync_mcp_tools(
        self,
        server_url: str,
        *,
        credential_secret_id: str | None = None,
        actor: ActorContext,
    ) -> dict[str, Any]: ...

    async def list_mcp_servers(self, *, actor: ActorContext) -> list[dict[str, Any]]: ...

    async def remove_mcp_server(
        self, server_url: str, *, actor: ActorContext
    ) -> None: ...


def build_mcp_sync_router(service: McpToolSyncServiceLike) -> APIRouter:
    """构造 MCP 工具同步 FastAPI ``APIRouter``。

    路由：
        - ``POST   /api/v1/mcp-servers/sync``：同步 MCP 服务器工具（DESIGNER/ADMIN）；
        - ``GET    /api/v1/mcp-servers``：列出已配置 MCP 服务器（tools:read）；
        - ``DELETE /api/v1/mcp-servers``：移除 MCP 服务器配置（DESIGNER/ADMIN）。

    说明：``server_url`` 作为 MCP 服务器自然主键，含 ``://`` 与路径段，不适合
    放入 URL 路径；故同步与删除均通过请求体传递 ``server_url``。

    :param service: ``McpToolSyncService`` 或鸭子类型对象。
    :returns: FastAPI ``APIRouter``，由调用方挂载到 app。
    """
    router = APIRouter(prefix="/api/v1/mcp-servers", tags=["mcp-servers"])

    @router.post(
        "/sync",
        response_model=SyncMcpToolsResultOut,
        status_code=status.HTTP_200_OK,
        responses={
            401: {"model": ErrorResponse, "description": "未认证"},
            403: {"model": ErrorResponse, "description": "权限不足"},
            422: {"model": ErrorResponse, "description": "server_url 非法"},
        },
    )
    async def sync_mcp_tools(  # noqa: ANN202
        payload: SyncMcpToolsPayload,
        actor: ActorDep,
    ) -> SyncMcpToolsResultOut | JSONResponse:
        """从 MCP 服务器发现工具并注册到 Tool Registry；不执行工具。"""
        try:
            result = await service.sync_mcp_tools(
                payload.server_url,
                credential_secret_id=payload.credential_secret_id,
                actor=actor,
            )
        except DomainError as exc:
            return _error_response(exc)
        return SyncMcpToolsResultOut(
            server_url=result["server_url"],
            synced=[SyncedToolOut(**t) for t in result["synced"]],
            skipped=list(result["skipped"]),
            errors=[SyncErrorOut(**e) for e in result["errors"]],
            synced_count=result["synced_count"],
            skipped_count=result["skipped_count"],
            error_count=result["error_count"],
        )

    @router.get(
        "",
        response_model=list[McpServerOut],
        status_code=status.HTTP_200_OK,
        responses={
            401: {"model": ErrorResponse, "description": "未认证"},
            403: {"model": ErrorResponse, "description": "权限不足"},
        },
    )
    async def list_mcp_servers(  # noqa: ANN202
        actor: ActorDep,
    ) -> list[McpServerOut] | JSONResponse:
        """列出全部已配置的 MCP 服务器。"""
        try:
            servers = await service.list_mcp_servers(actor=actor)
        except DomainError as exc:
            return _error_response(exc)
        return [McpServerOut(**s) for s in servers]

    @router.delete(
        "",
        response_model=None,
        status_code=status.HTTP_204_NO_CONTENT,
        responses={
            401: {"model": ErrorResponse, "description": "未认证"},
            403: {"model": ErrorResponse, "description": "权限不足"},
            404: {"model": ErrorResponse, "description": "MCP 服务器不存在"},
        },
    )
    async def remove_mcp_server(  # noqa: ANN202
        payload: RemoveMcpServerPayload,
        actor: ActorDep,
    ) -> Response | JSONResponse:
        """移除 MCP 服务器配置；保留已注册工具历史。"""
        try:
            await service.remove_mcp_server(payload.server_url, actor=actor)
        except DomainError as exc:
            return _error_response(exc)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


__all__ = [
    "ErrorResponse",
    "McpServerOut",
    "RemoveMcpServerPayload",
    "SyncErrorOut",
    "SyncMcpToolsPayload",
    "SyncMcpToolsResultOut",
    "SyncedToolOut",
    "ToolConfigurationHttpApi",
    "ToolListOut",
    "ToolMetadataPayload",
    "ToolRegistrationOut",
    "ToolVersionOut",
    "UnregisterToolOut",
    "build_mcp_sync_router",
    "build_tools_router",
]
