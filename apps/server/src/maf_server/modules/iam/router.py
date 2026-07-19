"""IAM 公共 HTTP 接口与 FastAPI 路由实现。

TASK-030 范围：
- 保留 ``IamHttpApi`` Protocol（其他任务接口契约）。
- 新增 ``build_auth_router`` 工厂，构造 FastAPI ``APIRouter``，挂载
  ``POST /api/v1/auth/login`` 与 ``POST /api/v1/auth/logout`` 两个端点；
- ``login`` 成功 200 返回 ``LoginResponse``，并设置 ``maf_session`` HttpOnly Cookie；
- ``logout`` 成功 204，清除 Cookie；幂等。

设计参考：《多 Agent 协同工具系统设计文档》5.3、9.1、11.1 节。
- 用户 API 使用 HttpOnly Session Cookie；
- Cookie 设置 ``HttpOnly``、``Secure``、``SameSite=Lax``；
- 登录失败统一 401，错误信息不区分用户是否存在；
- 密码与 token 不写日志、不进响应头。

本模块不实现认证中间件（``ActorContext`` 注入由后续任务负责）；
``logout`` 通过 FastAPI Depends 接受 actor，开发期可由测试 stub 提供。
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing_extensions import Annotated

from maf_contracts.common import ActorContext
from maf_domain.errors import UnauthenticatedError
from maf_server.api.dependencies import get_current_actor

from .schemas import (
    CreateUserRequest,
    LoginRequest,
    PutSettingRequest,
    SessionView,
    SettingView,
    UpdateUserRequest,
    UserPage,
    UserQuery,
    UserView,
)

# --------------------------------------------------------------------------- #
# Cookie 配置
# --------------------------------------------------------------------------- #

#: Session Cookie 名。前端不可读（HttpOnly），每次请求自动携带。
SESSION_COOKIE_NAME: str = "maf_session"

#: Cookie 默认属性。``Secure`` 在生产 HTTPS 环境启用；本地 HTTP 开发可通过
#: ``build_auth_router(secure_cookie=False)`` 关闭，便于浏览器实际接受 Cookie。
_DEFAULT_COOKIE_SAMESITE: str = "lax"
_DEFAULT_COOKIE_PATH: str = "/"


# --------------------------------------------------------------------------- #
# Pydantic 请求/响应模型（FastAPI 直接序列化；与 schemas.TypedDict 对齐）
# --------------------------------------------------------------------------- #


class LoginPayload(BaseModel):
    """``POST /api/v1/auth/login`` 请求体。"""

    username: str = Field(..., min_length=1, max_length=128, description="用户名")
    password: str = Field(..., min_length=1, max_length=256, description="明文密码")


class UserOut(BaseModel):
    """对外用户视图，不含密码哈希。"""

    id: str
    username: str
    display_name: str
    status: str
    permissions: list[str]
    version: int


class SessionOut(BaseModel):
    """``POST /api/v1/auth/login`` 成功响应体，与 ``LoginResponse`` 对齐。"""

    session_id: str
    expires_at: str
    token: str = Field(..., description="明文 Session Token，仅本次响应返回")
    user: UserOut


class ErrorResponse(BaseModel):
    """对外错误响应体，与 ``api.errors.ErrorResponse`` 对齐（精简版）。"""

    error_code: str
    message: str
    retryable: bool = False


# --------------------------------------------------------------------------- #
# Protocol（保留原有接口契约）
# --------------------------------------------------------------------------- #


class IamHttpApi(Protocol):
    async def post_login(self, request: LoginRequest) -> SessionView:
        """POST `/api/v1/auth/login`；成功 200，认证失败 401。"""
        ...

    async def post_logout(self, actor: ActorContext, session_id: str) -> None:
        """POST `/api/v1/auth/logout`；幂等撤销当前 session，成功 204。"""
        ...

    async def get_me(self, actor: ActorContext) -> UserView:
        """GET `/api/v1/me`；返回当前用户和服务端重新计算的权限。"""
        ...

    async def get_users(self, actor: ActorContext, query: UserQuery) -> UserPage:
        """GET `/api/v1/users`；管理员游标分页查询，成功 200。"""
        ...

    async def post_user(self, actor: ActorContext, request: CreateUserRequest) -> UserView:
        """POST `/api/v1/users`；创建成功 201，用户名冲突 409。"""
        ...

    async def patch_user(
        self, actor: ActorContext, user_id: str, request: UpdateUserRequest
    ) -> UserView:
        """PATCH `/api/v1/users/{id}`；按版本部分更新，成功 200。"""
        ...

    async def get_setting(self, actor: ActorContext, key: str) -> SettingView:
        """GET `/api/v1/settings/{key}`；未知 key 返回 404。"""
        ...

    async def put_setting(
        self, actor: ActorContext, key: str, request: PutSettingRequest
    ) -> SettingView:
        """PUT `/api/v1/settings/{key}`；创建或替换设置，成功 200。"""
        ...


# --------------------------------------------------------------------------- #
# ActorContext 依赖（占位实现；正式认证中间件在后续任务落地）
# --------------------------------------------------------------------------- #


async def _anonymous_actor_dependency(
    request: Request,
    maf_session: Annotated[str | None, Cookie()] = None,
) -> ActorContext:
    """从 ``maf_session`` Cookie 解析当前 actor。

    TASK-030 范围内不实现完整认证中间件；本函数作为 FastAPI Depends 占位，
    供 ``logout`` 端点取得 actor。正式实现应在 ``api/dependencies.py`` 中
    解析 Cookie/Authorization → 查 sessions 表 → 校验 user.status → 构造
    ``ActorContext``；失败抛 ``UnauthenticatedError``。

    测试通过 ``app.dependency_overrides`` 替换本函数注入 stub actor。
    """
    return await get_current_actor(request)


ActorDep = Annotated[ActorContext, Depends(_anonymous_actor_dependency)]


def _unauthenticated_response(message: str = "用户名或密码错误") -> JSONResponse:
    """构造 401 JSON 响应，与 ``api.errors`` 错误格式对齐。"""
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={
            "error": {
                "error_code": "UNAUTHENTICATED",
                "message": message,
                "retryable": False,
            }
        },
    )


def _build_session_out(session: SessionView) -> SessionOut:
    """把 ``SessionView`` TypedDict 映射为 Pydantic ``SessionOut``。"""
    user_dict = session["user"]
    return SessionOut(
        session_id=session["session_id"],
        expires_at=session["expires_at"],
        token=session.get("token", ""),
        user=UserOut(
            id=user_dict["id"],
            username=user_dict["username"],
            display_name=user_dict["display_name"],
            status=user_dict["status"],
            permissions=list(user_dict.get("permissions", [])),
            version=user_dict["version"],
        ),
    )


# --------------------------------------------------------------------------- #
# FastAPI Router 工厂
# --------------------------------------------------------------------------- #


def build_auth_router(
    service: "IamServiceLike",
    *,
    secure_cookie: bool = True,
    cookie_samesite: Literal["lax", "strict", "none"] = "lax",
) -> APIRouter:
    """构造 IAM 认证 FastAPI ``APIRouter``。

    路由：
        - ``POST /api/v1/auth/login``：验证密码，返回 token + 设置 Cookie；
        - ``POST /api/v1/auth/logout``：撤销会话，清除 Cookie。

    :param service: ``IamServiceImpl`` 或任何满足 ``login``/``logout`` 签名的对象。
    :param secure_cookie: Cookie 是否标记 ``Secure``。本地 HTTP 开发可设 False；
        生产必须 True。默认 True。
    :param cookie_samesite: Cookie ``SameSite`` 属性，默认 ``lax``。
    :returns: FastAPI ``APIRouter``，由调用方挂载到 app。
    """
    router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

    @router.post(
        "/login",
        response_model=SessionOut,
        status_code=status.HTTP_200_OK,
        responses={
            401: {"model": ErrorResponse, "description": "用户名或密码错误"},
        },
    )
    async def login(  # noqa: ANN202 - FastAPI 端点
        payload: LoginPayload,
        response: Response,
    ) -> SessionOut | JSONResponse:
        """本地登录：验证用户名密码，返回会话 token 并设置 HttpOnly Cookie。"""
        request_dict: LoginRequest = {
            "username": payload.username,
            "password": payload.password,
        }
        try:
            session = await service.login(request_dict)
        except UnauthenticatedError:
            # 不泄露具体原因；统一 401。
            return _unauthenticated_response()

        # 设置 HttpOnly Cookie；明文 token 同时在响应体返回一次，
        # 便于非浏览器客户端（如 CLI）从 JSON 读取。
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=session.get("token", ""),
            httponly=True,
            secure=secure_cookie,
            samesite=cookie_samesite,
            path=_DEFAULT_COOKIE_PATH,
            # 不设 max_age：会话 Cookie，浏览器关闭即失效；
            # 服务端按 sessions.expires_at 校验过期。
        )
        return _build_session_out(session)

    @router.post(
        "/logout",
        status_code=status.HTTP_204_NO_CONTENT,
        # response_model=None：明确告诉 FastAPI 不要从返回注解推断响应模型。
        # 204 不允许响应体；本端点直接返回 Response/JSONResponse 对象，由
        # FastAPI 原样透传，不经模型序列化。
        response_model=None,
        responses={
            401: {"model": ErrorResponse, "description": "未认证"},
        },
    )
    async def logout(  # noqa: ANN202 - FastAPI 端点
        actor: ActorDep,
        request: Request,
    ) -> Response | JSONResponse:
        """撤销当前会话；幂等。成功 204，清除 Cookie。"""
        # session_id 由请求体或查询参数提供；这里用 JSON body 兼容前端。
        try:
            body = await request.json()
        except Exception:
            body = {}
        session_id = ""
        if isinstance(body, dict):
            sid = body.get("session_id")
            if isinstance(sid, str):
                session_id = sid
        if not session_id:
            # 也允许从查询参数取得
            sid = request.query_params.get("session_id", "")
            if isinstance(sid, str):
                session_id = sid

        try:
            await service.logout(actor, session_id)
        except UnauthenticatedError:
            return _unauthenticated_response("未认证")

        # 清除 Cookie，无论 session_id 是否有效（幂等）。
        # 注意：必须在我们返回的 Response 上调用 delete_cookie，而非注入的
        # ``response`` 参数——FastAPI 直接使用返回的 Response 对象，注入的
        # ``response`` 仅用于在返回非 Response 对象时追加头/状态码。
        final_response = Response(status_code=status.HTTP_204_NO_CONTENT)
        final_response.delete_cookie(
            key=SESSION_COOKIE_NAME, path=_DEFAULT_COOKIE_PATH
        )
        return final_response

    return router


# --------------------------------------------------------------------------- #
# 用于类型注解的鸭子类型协议（仅 login/logout）
# --------------------------------------------------------------------------- #


class IamServiceLike(Protocol):
    """``build_auth_router`` 接受的 service 鸭子类型，只要求 login/logout。"""

    async def login(self, request: LoginRequest) -> SessionView: ...

    async def logout(self, actor: ActorContext, session_id: str) -> None: ...


# --------------------------------------------------------------------------- #
# 系统设置 HTTP 路由 —— TASK-032
# --------------------------------------------------------------------------- #


class SettingOut(BaseModel):
    """系统设置对外视图，与 ``schemas.SettingView`` 对齐。

    敏感设置的 ``value`` 始终为 ``None``；只通过 ``configured`` 与 ``fingerprint``
    暴露配置状态。明文绝不进入本模型。
    """

    key: str
    value: Any | None = None
    value_type: str
    is_secret: bool
    configured: bool
    fingerprint: str | None = None
    version: int
    updated_at: str
    updated_by: str


class PutSettingPayload(BaseModel):
    """``PUT /api/v1/settings/{key}`` 请求体。"""

    value: Any = Field(..., description="设置值；敏感设置为明文（仅本次请求传输）")
    expected_version: int | None = Field(
        None, description="乐观锁期望版本号；首次写入可省略"
    )
    idempotency_key: str = Field(..., description="幂等键（暂用于审计，未做强校验）")


class IamSettingsApi(Protocol):
    """``build_settings_router`` 接受的 service 鸭子类型，要求 get/put_setting。"""

    async def get_setting(self, actor: ActorContext, key: str) -> SettingView: ...

    async def put_setting(
        self, actor: ActorContext, key: str, request: PutSettingRequest
    ) -> SettingView: ...


def _build_setting_out(view: SettingView) -> SettingOut:
    """把 ``SettingView`` TypedDict 映射为 Pydantic ``SettingOut``。"""
    return SettingOut(
        key=view["key"],
        value=view.get("value"),
        value_type=view["value_type"],
        is_secret=view["is_secret"],
        configured=view["configured"],
        fingerprint=view.get("fingerprint"),
        version=view["version"],
        updated_at=view["updated_at"],
        updated_by=view["updated_by"],
    )


def build_settings_router(service: IamSettingsApi) -> APIRouter:
    """构造系统设置 FastAPI ``APIRouter``（TASK-032）。

    路由：
        - ``GET /api/v1/settings/{key}``：读取设置视图；未知 key 返回 400。
        - ``PUT /api/v1/settings/{key}``：创建或乐观锁更新；版本冲突返回 409。

    权限由 ``service`` 委托 ``PermissionService`` 校验：
        - 读：``read`` ``settings``（ADMIN/DESIGNER/OBSERVER）；
        - 写：``write`` ``settings``（仅 ADMIN）。
    非经授权的访问由 ``api.errors`` 映射为 401/403；未知 key 与版本冲突分别映射为
    400/409。敏感设置的明文只到达 ``service.put_setting``，绝不写入响应或日志。
    """
    router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

    @router.get(
        "/{key}",
        response_model=SettingOut,
        status_code=status.HTTP_200_OK,
        responses={
            400: {"model": ErrorResponse, "description": "未知 key"},
            401: {"model": ErrorResponse, "description": "未认证"},
            403: {"model": ErrorResponse, "description": "无 read 权限"},
        },
    )
    async def get_setting(  # noqa: ANN202 - FastAPI 端点
        key: str,
        actor: ActorDep,
    ) -> SettingOut:
        """读取系统设置视图；敏感设置只返回 configured/fingerprint。"""
        view = await service.get_setting(actor, key)
        return _build_setting_out(view)

    @router.put(
        "/{key}",
        response_model=SettingOut,
        status_code=status.HTTP_200_OK,
        responses={
            400: {"model": ErrorResponse, "description": "未知 key 或值非法"},
            401: {"model": ErrorResponse, "description": "未认证"},
            403: {"model": ErrorResponse, "description": "非 ADMIN，无 write 权限"},
            409: {"model": ErrorResponse, "description": "版本冲突"},
        },
    )
    async def put_setting(  # noqa: ANN202 - FastAPI 端点
        key: str,
        payload: PutSettingPayload,
        actor: ActorDep,
    ) -> SettingOut:
        """创建或替换系统设置；仅 ADMIN 可调用。"""
        request_dict: PutSettingRequest = {
            "value": payload.value,
            "expected_version": payload.expected_version,
            "idempotency_key": payload.idempotency_key,
        }
        view = await service.put_setting(actor, key, request_dict)
        return _build_setting_out(view)

    return router


def build_current_user_router(service: IamServiceLike) -> APIRouter:
    """Expose the authenticated user's current profile and permissions."""

    router = APIRouter(prefix="/api/v1", tags=["iam"])

    @router.get("/me", response_model=UserOut)
    async def get_me(actor: ActorDep) -> UserOut:
        return UserOut(**(await service.get_current_user(actor)))

    return router


__all__ = [
    "IamHttpApi",
    "IamSettingsApi",
    "LoginPayload",
    "PutSettingPayload",
    "SessionOut",
    "SettingOut",
    "UserOut",
    "SESSION_COOKIE_NAME",
    "build_auth_router",
    "build_current_user_router",
    "build_settings_router",
]
