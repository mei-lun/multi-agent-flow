"""Web 用户身份解析、密码哈希与会话 Token 工具。

根据《多 Agent 协同工具系统设计文档》5.3、7.1、11.1 节与 TASK-030：

- 用户 API 使用 HttpOnly Session Cookie；本模块提供密码哈希、Session Token
  生成与验证原语，供 ``modules.iam`` 应用服务层使用。
- 密码使用 bcrypt 强哈希存储（``bcrypt`` 库，rounds=12），明文密码绝不持久化、
  绝不写日志、绝不进入审计 payload。
- Session Token 使用 ``secrets.token_urlsafe(32)`` 生成（约 43 字符熵），
  数据库只保存其 SHA-256 哈希；验证时对调用方提供的 token 再次哈希并恒定时间比较。
- 恒定时间比较使用 ``hmac.compare_digest``，避免通过响应耗时区分用户是否存在
  或 token 是否有效（对应 TASK-030 验收"错误不区分用户不存在和密码错误"）。
- 跨节点身份由 Git 提交和节点清单验证，不经本模块；``IdentityService`` Protocol
  保留给后续认证中间件实现（解析 Cookie/Authorization 头并构造 ``ActorContext``）。

实现说明：直接使用 ``bcrypt`` 库而非 ``passlib``，因为 passlib 1.7.4 与
bcrypt 5.x 存在已知不兼容（``__about__`` 属性移除 + ``detect_wrap_bug`` 触发
72 字节限制异常）。``bcrypt`` 库 API 稳定且维护活跃，足以满足 TASK-030 需求。

本模块只依赖 Python 标准库（``secrets``、``hmac``、``hashlib``、``datetime``）
与 ``bcrypt``；不连接数据库，不接触网络，不读取业务表。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Protocol

import bcrypt

if TYPE_CHECKING:
    # 仅用于类型注解，避免运行时循环导入。
    from maf_contracts.common import ActorContext
    from maf_contracts.coordination import NodeManifest

# --------------------------------------------------------------------------- #
# 密码哈希
# --------------------------------------------------------------------------- #

#: bcrypt rounds（工作因子）。12 是 2025 年 OWASP 推荐下限，
#: 同时兼顾开发与测试速度。更换 rounds 需通过新迁移 + 重新哈希流程。
_BCRYPT_ROUNDS: int = 12

#: bcrypt 哈希前缀，用于校验从数据库读取的 ``password_hash`` 形态。
_BCRYPT_HASH_PREFIX: tuple[str, ...] = ("$2a$", "$2b$", "$2y$")

#: bcrypt 限制密码最大 72 字节；超过会被截断或抛错。本模块在哈希前显式截断，
#: 行为与 passlib 默认一致，避免 bcrypt 5.x 抛 ValueError。
_BCRYPT_MAX_PASSWORD_BYTES: int = 72

#: Session Token 字节数（``token_urlsafe`` 编码后约 43 字符）。
_SESSION_TOKEN_BYTES: int = 32

#: 默认 Session TTL（秒）。设计文档 11.1 节未给具体值，
#: 取 12 小时（43200 秒）作为 MVP 默认，可通过 ``IamServiceImpl`` 构造参数覆盖。
DEFAULT_SESSION_TTL_SECONDS: int = 12 * 60 * 60


def _truncate_for_bcrypt(plaintext: str) -> bytes:
    """把明文密码编码为 bytes 并截断到 72 字节（bcrypt 内部限制）。

    bcrypt 只使用密码前 72 字节；显式截断避免 bcrypt 5.x 在更长输入时抛
    ``ValueError``。UTF-8 编码后截断可能切断多字节字符，但 bcrypt 本就不
    保证多字节边界，截断行为与历史 passlib 默认一致。
    """
    raw = plaintext.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_PASSWORD_BYTES:
        raw = raw[:_BCRYPT_MAX_PASSWORD_BYTES]
    return raw


def hash_password(plaintext: str) -> str:
    """对明文密码做 bcrypt 强哈希，返回可直接存数据库的哈希字符串。

    谁调用它：
        ``IamServiceImpl.create_user`` / ``update_user`` 在写入 ``users`` 表前调用；
        ``seed_local_user`` 在测试与首次启动初始化管理员时调用。

    输入来源与可信度：
        ``plaintext`` 来自管理员请求体或登录后修改密码流程，已在内存中校验过
        长度与复杂度；本函数不重复业务校验，只负责安全哈希。

    安全约束：
        - 明文 ``plaintext`` 不写日志、不进审计、不进 Outbox；
        - 返回值是 ``$2b$12$...`` 形态的哈希字符串，可持久化；
        - 同一明文每次哈希结果不同（bcrypt 自带 salt），不可用于直接比较；
        - 超过 72 字节的密码在哈希前截断（bcrypt 内部限制）。

    :param plaintext: 明文密码。空字符串会被拒绝。
    :returns: bcrypt 哈希字符串（UTF-8 解码）。
    """
    if not plaintext:
        # 防御性校验：空密码不应进入哈希流程，避免静默生成"空密码"账号。
        raise ValueError("密码不能为空")
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    hashed = bcrypt.hashpw(_truncate_for_bcrypt(plaintext), salt)
    return hashed.decode("utf-8")


def verify_password(plaintext: str, password_hash: str) -> bool:
    """恒定时间验证明文密码是否匹配已存储的 bcrypt 哈希。

    谁调用它：
        ``IamServiceImpl.login`` 在读取用户 ``password_hash`` 后调用。

    输入来源与可信度：
        - ``plaintext`` 来自 ``LoginRequest``，不可信；
        - ``password_hash`` 来自 ``users.password_hash`` 列，可信但可能为 NULL。

    安全约束：
        - 返回 ``bool``，不抛异常（除非 ``password_hash`` 格式严重损坏）；
        - 调用方对"用户不存在"和"密码错误"必须返回相同错误码与近似耗时，
          避免用户枚举；本函数对 NULL/空哈希返回 ``False`` 而非抛错，
          便于上层在"用户不存在"分支也走一次恒定时间比较；
        - 明文 ``plaintext`` 不写日志；
        - 超过 72 字节的密码在验证前截断，与 ``hash_password`` 行为一致。

    :param plaintext: 用户提交的明文密码。
    :param password_hash: 数据库存储的 bcrypt 哈希。``None``/空串视为不匹配。
    :returns: 匹配返回 ``True``，否则 ``False``。
    """
    if not plaintext or not password_hash:
        return False
    if not password_hash.startswith(_BCRYPT_HASH_PREFIX):
        # 哈希格式异常：视为不匹配而非抛错，让上层统一返回认证失败，
        # 避免通过错误形态泄露用户是否存在或哈希是否损坏。
        return False
    try:
        return bcrypt.checkpw(
            _truncate_for_bcrypt(plaintext),
            password_hash.encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


def is_password_hash(value: str | None) -> bool:
    """判断字符串是否为 bcrypt 哈希形态，用于测试与防御性校验。

    不解析哈希、不验证 salt 与 rounds；只检查前缀。
    """
    if not value:
        return False
    return value.startswith(_BCRYPT_HASH_PREFIX)


# --------------------------------------------------------------------------- #
# Session Token 生成与验证
# --------------------------------------------------------------------------- #


def generate_session_token() -> str:
    """生成新的 Session Token（URL-safe base64，约 43 字符）。

    谁调用它：
        ``IamServiceImpl.login`` 创建新会话时调用。

    安全约束：
        - 使用 ``secrets.token_urlsafe``（CSPRNG），熵足够抵御穷举；
        - 返回的明文 token 只在登录响应中返回一次；数据库只保存其哈希；
        - 明文 token 不写日志、不进审计 payload。

    :returns: URL-safe base64 编码的随机 token。
    """
    return secrets.token_urlsafe(_SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """对 Session Token 做 SHA-256 哈希，返回十六进制摘要。

    谁调用它：
        - ``IamServiceImpl.login`` 在持久化 session 前调用，把哈希写入
          ``sessions.token_hash`` 列；
        - ``IdentityService.authenticate_session`` 在验证时对调用方提供的
          token 再次哈希，与库中哈希比较。

    为什么用 SHA-256 而非 bcrypt：
        Session Token 已是高熵随机串（32 字节），不需要慢哈希防穷举；
        SHA-256 查询性能足以支撑每次请求验证，且数据库索引友好。
        密码低熵必须用 bcrypt；Token 高熵用 SHA-256 即可。

    安全约束：
        - 明文 token 不进入数据库；
        - 调用方传入空串时返回空串哈希的固定值，避免异常路径泄露；
        - 不写日志。

    :param token: ``generate_session_token`` 生成的明文 token。
    :returns: 64 字符小写十六进制 SHA-256 摘要。
    """
    if not token:
        raise ValueError("session token 不能为空")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_session_token(token: str, token_hash: str) -> bool:
    """恒定时间验证 Session Token 是否匹配存储的哈希。

    谁调用它：
        ``IdentityService.authenticate_session`` 实现调用，验证 Cookie 或
        Authorization 头中的 token。

    安全约束：
        - 使用 ``hmac.compare_digest`` 恒定时间比较，避免计时侧信道；
        - 空 token 或空哈希返回 ``False``，不抛异常；
        - 不写日志。

    :param token: 调用方提供的明文 token。
    :param token_hash: 数据库存储的 SHA-256 哈希。
    :returns: 匹配返回 ``True``，否则 ``False``。
    """
    if not token or not token_hash:
        return False
    try:
        computed = hash_session_token(token)
    except ValueError:
        return False
    return hmac.compare_digest(computed, token_hash)


# --------------------------------------------------------------------------- #
# 会话过期计算
# --------------------------------------------------------------------------- #


def compute_session_expiry(
    now: datetime, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
) -> datetime:
    """计算会话过期时间（带 UTC 时区）。

    谁调用它：
        ``IamServiceImpl.login`` 在创建 session 时调用。

    输入来源与可信度：
        - ``now`` 由 ``Clock`` 提供，测试中可注入虚拟时钟；
        - ``ttl_seconds`` 由 ``ServerSettings`` 或 ``IamServiceImpl`` 构造参数提供。

    约束：
        - ``now`` 必须带时区；若为 naive datetime，按 UTC 解释以避免歧义；
        - ``ttl_seconds`` 必须 > 0；
        - 返回值带 UTC 时区，序列化为 ISO 8601 字符串时以 ``Z`` 结尾。

    :param now: 当前时间（建议带 UTC 时区）。
    :param ttl_seconds: 会话存活秒数，默认 12 小时。
    :returns: 过期时间（带 UTC 时区）。
    """
    if ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds 必须为正数，got {ttl_seconds}")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now + timedelta(seconds=ttl_seconds)


def is_expired(expires_at: datetime, now: datetime) -> bool:
    """判断会话是否已过期。

    两者都应为带时区 datetime；naive datetime 按 UTC 解释。
    """
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now >= expires_at


# --------------------------------------------------------------------------- #
# IdentityService Protocol（保留给认证中间件实现）
# --------------------------------------------------------------------------- #


class IdentityService(Protocol):
    """Web 用户身份解析协议。

    具体实现（认证中间件）将在后续任务落地：解析 ``maf_session`` Cookie 或
    ``Authorization`` 头中的 Session Token，查 ``sessions`` 表与 ``users`` 表，
    校验未过期、未撤销、用户 ACTIVE，然后构造 ``ActorContext``。失败不返回
    部分身份，统一抛 ``UnauthenticatedError``。
    """

    async def authenticate_session(self, session_token: str) -> ActorContext:
        """验证签名/会话/用户状态并构造服务端权限上下文；失败不返回部分身份。"""
        ...


# --------------------------------------------------------------------------- #
# TASK-020: 节点 Git 身份验证辅助函数
# --------------------------------------------------------------------------- #


def extract_node_identity_from_manifest(manifest: "NodeManifest") -> dict[str, str]:
    """从节点清单提取 Git 提交身份（``name`` + ``email``）。

    谁调用它：
        ``LocalGitCoordinationService.verify_node_identity`` 在比较事件 commit
        author 与已注册节点声明身份前调用。

    输入来源与可信度：
        - ``manifest`` 来自 ``maf/control:.maf/nodes/<node-id>.yaml``（中央调度器
          写入，可信），或 ``NODE_REGISTERED`` 事件 ``payload.manifest``（节点
          自声明，首次注册时使用，需经 commit author 验证后才入库）。

    约束：
        - ``manifest.git_identity`` 必须含 ``name`` 和 ``email``；缺失返回空串；
        - 不写日志、不进审计；
        - 返回 dict 可直接与 ``git log --format=%an%n%ae`` 输出比较。

    :param manifest: 节点清单（``NodeManifest`` TypedDict 或同类 dict）。
    :returns: ``{"name": ..., "email": ...}``，缺失字段为空串。
    """
    git_identity = (manifest or {}).get("git_identity") or {}
    if not isinstance(git_identity, dict):
        return {"name": "", "email": ""}
    return {
        "name": str(git_identity.get("name") or ""),
        "email": str(git_identity.get("email") or ""),
    }


def verify_commit_author(
    commit_author: dict[str, str],
    declared_identity: dict[str, str],
) -> bool:
    """验证 commit author email 与节点声明身份一致（MVP 策略）。

    谁调用它：
        ``LocalGitCoordinationService.verify_node_identity`` 在读取事件 commit
        author 后调用。

    MVP 策略（对应 TASK-020「签名验证可降级为 commit author 验证」）：
        - 只比较 email（commit author email 与 manifest.git_identity.email）；
        - email 大小写敏感（与 git 内部行为一致）；
        - 完整 GPG/SSH 签名验证由后续任务增强（TASK-020 明确不包含密钥签发）。

    安全约束：
        - 不写日志（email 不是凭据但避免审计噪音）；
        - email 比较非恒定时间（email 不是高熵秘密，无需防计时侧信道）；
        - 空 email 视为不匹配（防御性：未声明的身份不能通过验证）。

    :param commit_author: ``{"name": ..., "email": ...}``，来自 ``git log``。
    :param declared_identity: ``{"name": ..., "email": ...}``，来自 manifest。
    :returns: email 一致返回 ``True``，否则 ``False``。
    """
    commit_email = (commit_author or {}).get("email") or ""
    declared_email = (declared_identity or {}).get("email") or ""
    if not commit_email or not declared_email:
        return False
    return commit_email == declared_email


__all__ = [
    "DEFAULT_SESSION_TTL_SECONDS",
    "IdentityService",
    "compute_session_expiry",
    "extract_node_identity_from_manifest",
    "generate_session_token",
    "hash_password",
    "hash_session_token",
    "is_expired",
    "is_password_hash",
    "verify_commit_author",
    "verify_password",
    "verify_session_token",
]
