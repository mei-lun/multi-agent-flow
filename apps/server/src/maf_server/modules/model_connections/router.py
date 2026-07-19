"""模型连接配置管理 HTTP 接口与 FastAPI 路由实现。

TASK-037 范围：
- 新增 ``build_model_connection_router`` 工厂，构造 FastAPI ``APIRouter``，挂载
  ``POST``/``GET``/``PATCH``/``DELETE`` ``/api/v1/model-connections`` 与
  ``POST /api/v1/model-connections/{id}/test`` 共 6 个端点；
- 路由只做请求体解析与 Service 转发，权限校验与事务边界全部由
  ``ModelConnectionServiceImpl`` 承担；
- 凭据明文只存在于请求体内（仅本次请求传输），绝不进入响应、日志或路径参数。

设计参考：《接口设计与实现规范》第 4 节、《多 Agent 协同工具系统设计文档》§9。
- 创建连接 201，列表 200，详情 200，更新 200，删除 204，测试 200；
- 参数错误 400；未认证 401；无权限 403；不存在 404；版本冲突 409；
- ``credential_value`` 字段绝不进入响应；``ModelConnectionView`` 只返回
  ``credential_type`` 与不可逆 ``credential_fingerprint``。

ActorContext 注入：本模块不实现认证中间件，``_anonymous_actor_dependency``
作为 FastAPI Depends 占位；正式实现应在 ``api/dependencies.py`` 中解析
Cookie/Authorization → 构造 ``ActorContext``；失败抛 ``UnauthenticatedError``。
测试通过 ``app.dependency_overrides`` 替换本函数注入 stub actor。
"""

from __future__ import annotations

from typing import Protocol

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field
from typing_extensions import Annotated

from maf_contracts.common import ActorContext
from maf_domain.errors import UnauthenticatedError
from maf_server.api.dependencies import get_current_actor

from .schemas import ModelConnectionView, TestResult
from .service import ModelConnectionService

# --------------------------------------------------------------------------- #
# Pydantic 请求/响应模型（FastAPI 直接序列化；与 schemas.TypedDict 对齐）
# --------------------------------------------------------------------------- #


class ModelConnectionOut(BaseModel):
    """模型连接对外视图，与 ``ModelConnectionView`` 对齐。

    安全约束：``credential_value`` 绝不进入本模型；``credential_fingerprint``
    是不可逆指纹，仅供运维识别与脱敏展示。
    """

    id: str
    name: str
    provider: str
    model_id: str
    api_base: str
    credential_type: str
    credential_fingerprint: str | None = None
    status: str
    created_by: str
    created_at: str
    updated_at: str
    version: int


class CreateModelConnectionPayload(BaseModel):
    """``POST /api/v1/model-connections`` 请求体。"""

    name: str = Field(..., min_length=1, max_length=128, description="连接名称")
    provider: str = Field(
        ..., description="供应商：openai/anthropic/azure/local"
    )
    model_id: str = Field(..., min_length=1, max_length=256, description="模型 ID")
    api_base: str = Field(..., min_length=1, description="API 基础 URL")
    credential_type: str = Field(
        ..., description="凭据类型：api_key/oauth_token/bearer_token"
    )
    credential_value: str = Field(
        ..., min_length=1, description="明文凭据；仅本次请求传输，绝不持久化明文"
    )
    idempotency_key: str = Field(
        ..., description="幂等键（暂用于审计，未做强校验）"
    )


class UpdateModelConnectionPayload(BaseModel):
    """``PATCH /api/v1/model-connections/{id}`` 请求体。

    至少提供 ``name``/``api_base``/``credential_value`` 之一；``expected_version``
    为必填的乐观锁版本号。``credential_value`` 非空时经 SecretService 轮换。
    """

    name: str | None = Field(None, description="新连接名称")
    api_base: str | None = Field(None, description="新 api_base URL")
    credential_value: str | None = Field(
        None, description="新明文凭据；非空时经 SecretService 轮换"
    )
    expected_version: int = Field(..., description="乐观锁期望版本号")
    idempotency_key: str = Field(..., description="幂等键")


class TestResultOut(BaseModel):
    """``POST /api/v1/model-connections/{id}/test`` 响应体，与 ``TestResult`` 对齐。"""

    connection_id: str
    ok: bool
    status: str
    message: str
    checked_at: str


class ErrorResponse(BaseModel):
    """对外错误响应体，与 ``api.errors.ErrorResponse`` 对齐（精简版）。"""

    error_code: str
    message: str
    retryable: bool = False


# --------------------------------------------------------------------------- #
# ActorContext 依赖（占位实现；正式认证中间件在后续任务落地）
# --------------------------------------------------------------------------- #


async def _anonymous_actor_dependency(request: Request) -> ActorContext:
    """占位依赖：解析当前 actor；正式实现在 ``api/dependencies.py``。

    TASK-037 范围内不实现完整认证中间件；本函数作为 FastAPI Depends 占位，
    供路由取得 actor。正式实现应解析 Cookie/Authorization → 查 sessions 表 →
    校验 user.status → 构造 ``ActorContext``；失败抛 ``UnauthenticatedError``。

    测试通过 ``app.dependency_overrides`` 替换本函数注入 stub actor。
    """
    return await get_current_actor(request)


ActorDep = Annotated[ActorContext, Depends(_anonymous_actor_dependency)]


# --------------------------------------------------------------------------- #
# 视图转换
# --------------------------------------------------------------------------- #


def _build_view_out(view: ModelConnectionView) -> ModelConnectionOut:
    """把 ``ModelConnectionView`` TypedDict 映射为 Pydantic ``ModelConnectionOut``。"""
    return ModelConnectionOut(
        id=view["id"],
        name=view["name"],
        provider=view["provider"],
        model_id=view["model_id"],
        api_base=view["api_base"],
        credential_type=view["credential_type"],
        credential_fingerprint=view.get("credential_fingerprint"),
        status=view["status"],
        created_by=view["created_by"],
        created_at=view["created_at"],
        updated_at=view["updated_at"],
        version=view["version"],
    )


def _build_test_result_out(result: TestResult) -> TestResultOut:
    """把 ``TestResult`` TypedDict 映射为 Pydantic ``TestResultOut``。"""
    return TestResultOut(
        connection_id=result["connection_id"],
        ok=result["ok"],
        status=result["status"],
        message=result["message"],
        checked_at=result["checked_at"],
    )


# --------------------------------------------------------------------------- #
# 用于类型注解的鸭子类型协议
# --------------------------------------------------------------------------- #


class ModelConnectionServiceLike(Protocol):
    """``build_model_connection_router`` 接受的 service 鸭子类型。

    与 ``ModelConnectionService`` Protocol 字段对齐；只要求本路由使用的方法存在。
    """

    async def create_connection(
        self,
        name: str,
        provider: str,
        model_id: str,
        api_base: str,
        credential_type: str,
        credential_value: str,
        *,
        actor: ActorContext,
    ) -> ModelConnectionView: ...

    async def get_connection(
        self, connection_id: str, *, actor: ActorContext
    ) -> ModelConnectionView: ...

    async def list_connections(
        self, *, actor: ActorContext
    ) -> list[ModelConnectionView]: ...

    async def update_connection(
        self,
        connection_id: str,
        *,
        name: str | None = ...,
        api_base: str | None = ...,
        credential_value: str | None = ...,
        expected_version: int,
        actor: ActorContext,
    ) -> ModelConnectionView: ...

    async def delete_connection(
        self,
        connection_id: str,
        expected_version: int,
        *,
        actor: ActorContext,
    ) -> None: ...

    async def test_connection(
        self, connection_id: str, *, actor: ActorContext
    ) -> TestResult: ...


# --------------------------------------------------------------------------- #
# FastAPI Router 工厂
# --------------------------------------------------------------------------- #


def build_model_connection_router(
    service: ModelConnectionServiceLike,
) -> APIRouter:
    """构造模型连接配置 FastAPI ``APIRouter``（TASK-037）。

    路由：
        - ``POST /api/v1/model-connections``：创建连接，成功 201；
        - ``GET /api/v1/model-connections``：列表，成功 200；
        - ``GET /api/v1/model-connections/{connection_id}``：详情，成功 200；
        - ``PATCH /api/v1/model-connections/{connection_id}``：更新，成功 200；
        - ``DELETE /api/v1/model-connections/{connection_id}``：删除，成功 204；
        - ``POST /api/v1/model-connections/{connection_id}/test``：测试，成功 200。

    权限由 ``service`` 委托 ``PermissionService`` 校验：
        - 读（get/list/test）：``read`` ``model_connections``（ADMIN/DESIGNER/OBSERVER）；
        - 写（create/update/delete）：``write`` ``model_connections``（ADMIN/DESIGNER）。
    非经授权的访问由 ``api.errors`` 映射为 401/403；版本冲突映射为 409；
    不存在映射为 404；参数错误映射为 400。

    :param service: ``ModelConnectionServiceImpl`` 或任何满足签名鸭子类型的对象。
    :returns: FastAPI ``APIRouter``，由调用方挂载到 app。
    """
    router = APIRouter(prefix="/api/v1/model-connections", tags=["model-connections"])

    @router.post(
        "",
        response_model=ModelConnectionOut,
        status_code=status.HTTP_201_CREATED,
        responses={
            400: {"model": ErrorResponse, "description": "参数非法"},
            401: {"model": ErrorResponse, "description": "未认证"},
            403: {"model": ErrorResponse, "description": "非 ADMIN/DESIGNER，无 write 权限"},
        },
    )
    async def create_connection(  # noqa: ANN202 - FastAPI 端点
        payload: CreateModelConnectionPayload,
        actor: ActorDep,
    ) -> ModelConnectionOut:
        """创建模型连接；仅 ADMIN/DESIGNER。响应不含 credential_value。"""
        view = await service.create_connection(
            name=payload.name,
            provider=payload.provider,
            model_id=payload.model_id,
            api_base=payload.api_base,
            credential_type=payload.credential_type,
            credential_value=payload.credential_value,
            actor=actor,
        )
        return _build_view_out(view)

    @router.get(
        "",
        response_model=list[ModelConnectionOut],
        status_code=status.HTTP_200_OK,
        responses={
            401: {"model": ErrorResponse, "description": "未认证"},
            403: {"model": ErrorResponse, "description": "无 read 权限"},
        },
    )
    async def list_connections(  # noqa: ANN202 - FastAPI 端点
        actor: ActorDep,
    ) -> list[ModelConnectionOut]:
        """列出所有连接，按创建时间升序。"""
        views = await service.list_connections(actor=actor)
        return [_build_view_out(v) for v in views]

    @router.get(
        "/{connection_id}",
        response_model=ModelConnectionOut,
        status_code=status.HTTP_200_OK,
        responses={
            400: {"model": ErrorResponse, "description": "connection_id 非法"},
            401: {"model": ErrorResponse, "description": "未认证"},
            403: {"model": ErrorResponse, "description": "无 read 权限"},
            404: {"model": ErrorResponse, "description": "连接不存在"},
        },
    )
    async def get_connection(  # noqa: ANN202 - FastAPI 端点
        connection_id: str,
        actor: ActorDep,
    ) -> ModelConnectionOut:
        """获取连接详情；响应不含 credential_value 与 secret_id。"""
        view = await service.get_connection(connection_id, actor=actor)
        return _build_view_out(view)

    @router.patch(
        "/{connection_id}",
        response_model=ModelConnectionOut,
        status_code=status.HTTP_200_OK,
        responses={
            400: {"model": ErrorResponse, "description": "参数非法"},
            401: {"model": ErrorResponse, "description": "未认证"},
            403: {"model": ErrorResponse, "description": "非 ADMIN/DESIGNER"},
            404: {"model": ErrorResponse, "description": "连接不存在"},
            409: {"model": ErrorResponse, "description": "版本冲突"},
        },
    )
    async def update_connection(  # noqa: ANN202 - FastAPI 端点
        connection_id: str,
        payload: UpdateModelConnectionPayload,
        actor: ActorDep,
    ) -> ModelConnectionOut:
        """更新连接；仅 ADMIN/DESIGNER。更新凭据则经 SecretService 轮换。"""
        view = await service.update_connection(
            connection_id,
            name=payload.name,
            api_base=payload.api_base,
            credential_value=payload.credential_value,
            expected_version=payload.expected_version,
            actor=actor,
        )
        return _build_view_out(view)

    @router.delete(
        "/{connection_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_model=None,
        responses={
            400: {"model": ErrorResponse, "description": "参数非法"},
            401: {"model": ErrorResponse, "description": "未认证"},
            403: {"model": ErrorResponse, "description": "非 ADMIN/DESIGNER"},
            404: {"model": ErrorResponse, "description": "连接不存在"},
            409: {"model": ErrorResponse, "description": "版本冲突"},
        },
    )
    async def delete_connection(  # noqa: ANN202 - FastAPI 端点
        connection_id: str,
        actor: ActorDep,
        expected_version: int = Query(
            ..., description="乐观锁期望版本号；与 PATCH 一致以避免误删"
        ),
    ) -> Response:
        """删除连接；仅 ADMIN/DESIGNER。同时 best-effort 删除凭据。

        ``expected_version`` 通过查询参数提供（与 PATCH 一致），以避免误删与
        并发覆盖。DELETE 请求体在不同客户端实现差异较大，使用查询参数更稳妥。
        """
        await service.delete_connection(
            connection_id,
            expected_version,
            actor=actor,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/{connection_id}/test",
        response_model=TestResultOut,
        status_code=status.HTTP_200_OK,
        responses={
            400: {"model": ErrorResponse, "description": "connection_id 非法"},
            401: {"model": ErrorResponse, "description": "未认证"},
            403: {"model": ErrorResponse, "description": "无 read 权限"},
            404: {"model": ErrorResponse, "description": "连接不存在"},
        },
    )
    async def test_connection(  # noqa: ANN202 - FastAPI 端点
        connection_id: str,
        actor: ActorDep,
    ) -> TestResultOut:
        """测试连接配置完整性（不含推理调用）。"""
        result = await service.test_connection(connection_id, actor=actor)
        return _build_test_result_out(result)

    return router


__all__ = [
    "ActorDep",
    "CreateModelConnectionPayload",
    "ErrorResponse",
    "ModelConnectionOut",
    "ModelConnectionServiceLike",
    "TestResultOut",
    "UpdateModelConnectionPayload",
    "_anonymous_actor_dependency",
    "build_model_connection_router",
]
