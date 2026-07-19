"""Stable domain error codes, exception types, and Git event reason codes.

错误体系说明：
- ``ErrorCode``：稳定字符串错误码，与 HTTP 状态解耦，供 API、审计、日志和
  供应商 Adapter 统一引用。新增错误码只能追加，禁止重命名或复用旧值。
- ``ReasonCode``：Git 协调 ``EventDecision`` 拒绝原因的稳定枚举。中央调度器
  ``record_event_decision`` 必须使用此处定义的取值，不能写自由文本，以便
  节点按确定原因重试或转人工（详见《GitHub 分布式协作协议》第 6、11 节）。
- ``DomainError``：业务抛出的领域异常基类，携带 ``error_code``、``reason_code``、
  ``message``、``context``、``retryable`` 五个稳定字段；子类按错误大类划分，
  供 API 层映射 HTTP 状态和构造 ``ErrorResponse``。

本文件不依赖 FastAPI、SQLAlchemy、LangGraph、Docker 或模型 SDK。
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """稳定错误码。值与 HTTP 状态解耦，禁止重命名或复用。"""

    # 参数与 Schema 校验类（4xx）
    ARGUMENT_INVALID = "ARGUMENT_INVALID"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    STATE_TRANSITION_INVALID = "STATE_TRANSITION_INVALID"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"

    # 身份与权限类
    UNAUTHENTICATED = "UNAUTHENTICATED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    TOOL_POLICY_DENIED = "TOOL_POLICY_DENIED"

    # 资源状态类
    NOT_FOUND = "NOT_FOUND"
    ALREADY_EXISTS = "ALREADY_EXISTS"

    # 版本与并发冲突类（409）
    VERSION_CONFLICT = "VERSION_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"

    # Git 协调冲突类（409）
    GIT_CONFLICT = "GIT_CONFLICT"
    GIT_EVENT_REJECTED = "GIT_EVENT_REJECTED"

    # 限流与外部依赖类（429 / 5xx）
    RATE_LIMITED = "RATE_LIMITED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    EXTERNAL_DEPENDENCY_FAILED = "EXTERNAL_DEPENDENCY_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ReasonCode(str, Enum):
    """Git ``EventDecision`` 拒绝原因的稳定枚举。

    Git 协调协议要求 ``EventDecision`` 拒绝原因使用稳定 reason_code，不能是
    自由文本。中央调度器 ``record_event_decision`` 与节点事件分支处理器
    必须使用此处定义的取值，以便节点按确定原因重试或转人工。
    取值与名字保持一致，保证跨版本稳定。
    """

    # 事件级原因（对应 Git 协议第 6、7、11 节中央对节点事件的处理）
    EVENT_DUPLICATE = "EVENT_DUPLICATE"
    EVENT_SCHEMA_INVALID = "EVENT_SCHEMA_INVALID"
    EVENT_NODE_UNKNOWN = "EVENT_NODE_UNKNOWN"
    EVENT_NODE_IDENTITY_MISMATCH = "EVENT_NODE_IDENTITY_MISMATCH"
    EVENT_ASSIGNMENT_UNKNOWN = "EVENT_ASSIGNMENT_UNKNOWN"
    EVENT_EPOCH_STALE = "EVENT_EPOCH_STALE"
    EVENT_TASK_NOT_ASSIGNED = "EVENT_TASK_NOT_ASSIGNED"
    EVENT_TASK_STATE_INVALID = "EVENT_TASK_STATE_INVALID"
    EVENT_CAPABILITY_MISMATCH = "EVENT_CAPABILITY_MISMATCH"
    EVENT_BASE_COMMIT_MISMATCH = "EVENT_BASE_COMMIT_MISMATCH"
    EVENT_CONTROL_COMMIT_BEHIND = "EVENT_CONTROL_COMMIT_BEHIND"
    EVENT_DEPENDENCIES_INCOMPLETE = "EVENT_DEPENDENCIES_INCOMPLETE"
    EVENT_CAPACITY_FULL = "EVENT_CAPACITY_FULL"
    EVENT_LOST_TO_HIGHER_PRIORITY = "EVENT_LOST_TO_HIGHER_PRIORITY"
    EVENT_NODE_OFFLINE = "EVENT_NODE_OFFLINE"

    # Policy 原因（对应系统设计 11.5 CapabilityDecision、15.2.1 EmbeddedPolicyEngine）
    POLICY_CASBIN_DENIED = "POLICY_CASBIN_DENIED"
    POLICY_PATH_DENIED = "POLICY_PATH_DENIED"
    POLICY_NETWORK_DENIED = "POLICY_NETWORK_DENIED"
    POLICY_BUDGET_DENIED = "POLICY_BUDGET_DENIED"


class DomainError(Exception):
    """领域错误基类。

    所有业务异常都应派生自本类，携带以下稳定字段：
    - ``error_code``：``ErrorCode`` 枚举值，与 HTTP 状态解耦；
    - ``reason_code``：稳定子原因字符串，用于 ``EventDecision`` 和审计表
      ``reason_code`` 列。建议使用 ``ReasonCode`` 枚举；
    - ``message``：人类可读错误说明（不含敏感信息和堆栈）；
    - ``context``：附加键值对（如 ``task_id``、``assignment_epoch``），可空；
    - ``retryable``：调用方是否可按相同参数重试。

    本类不读取 ``__traceback__``，也不在 ``__str__`` 之外输出堆栈信息。
    """

    error_code: ErrorCode = ErrorCode.INTERNAL_ERROR
    default_reason_code: ReasonCode | None = None
    default_retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        reason_code: ReasonCode | str | None = None,
        context: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message: str = message
        if reason_code is None:
            self.reason_code: str | None = (
                self.default_reason_code.value if self.default_reason_code is not None else None
            )
        elif isinstance(reason_code, ReasonCode):
            self.reason_code = reason_code.value
        else:
            self.reason_code = reason_code
        self.context: dict[str, Any] = dict(context) if context else {}
        self.retryable: bool = self.default_retryable if retryable is None else retryable

    def to_dict(self) -> dict[str, Any]:
        """返回不含堆栈的稳定字典表示，用于日志和审计。"""
        return {
            "error_code": self.error_code.value,
            "reason_code": self.reason_code,
            "message": self.message,
            "retryable": self.retryable,
            "context": dict(self.context),
        }


# --------------------------------------------------------------------------- #
# 参数与 Schema 校验类
# --------------------------------------------------------------------------- #


class ArgumentError(DomainError):
    """参数缺失、类型错误或取值非法。HTTP 400。"""

    error_code = ErrorCode.ARGUMENT_INVALID
    default_retryable = False


class ValidationError(DomainError):
    """请求体 Schema 校验未通过。HTTP 422。"""

    error_code = ErrorCode.SCHEMA_VALIDATION_FAILED
    default_retryable = False


class UnsupportedOperationError(DomainError):
    """请求的操作在当前上下文不被支持（如 UNSUPPORTED_NODE_TYPE）。HTTP 400。

    对应系统设计 25.9 ``raise DomainError("UNSUPPORTED_NODE_TYPE", retryable=False)``。
    """

    error_code = ErrorCode.UNSUPPORTED_OPERATION
    default_retryable = False


# --------------------------------------------------------------------------- #
# 身份与权限类
# --------------------------------------------------------------------------- #


class UnauthenticatedError(DomainError):
    """未提供有效身份。HTTP 401。"""

    error_code = ErrorCode.UNAUTHENTICATED
    default_retryable = False


class PermissionDeniedError(DomainError):
    """RBAC/ABAC 拒绝。HTTP 403。"""

    error_code = ErrorCode.PERMISSION_DENIED
    default_retryable = False


class ToolPolicyDeniedError(DomainError):
    """Tool 调用被 PolicyEngine 拒绝，携带 ``reason_code``。HTTP 403。

    ``reason_code`` 必须是 ``ReasonCode.POLICY_*`` 取值或 CapabilityDecision
    返回的稳定字符串。对应系统设计 25.13 ``raise ToolPolicyDenied(decision.reason_code)``。
    """

    error_code = ErrorCode.TOOL_POLICY_DENIED
    default_retryable = False


# --------------------------------------------------------------------------- #
# 资源状态类
# --------------------------------------------------------------------------- #


class NotFoundError(DomainError):
    """资源不存在。HTTP 404。"""

    error_code = ErrorCode.NOT_FOUND
    default_retryable = False


class AlreadyExistsError(DomainError):
    """资源已存在且违反唯一性。HTTP 409。"""

    error_code = ErrorCode.ALREADY_EXISTS
    default_retryable = False


# --------------------------------------------------------------------------- #
# 版本与并发冲突类
# --------------------------------------------------------------------------- #


class VersionConflictError(DomainError):
    """乐观锁 ``expected_version`` 不匹配。HTTP 409。"""

    error_code = ErrorCode.VERSION_CONFLICT
    default_retryable = False


class IdempotencyConflictError(DomainError):
    """相同 Idempotency-Key 但请求体不同。HTTP 409。"""

    error_code = ErrorCode.IDEMPOTENCY_CONFLICT
    default_retryable = False


# --------------------------------------------------------------------------- #
# Git 协调冲突类
# --------------------------------------------------------------------------- #


class GitConflictError(DomainError):
    """Git push 冲突、expected head 不匹配或 control fast-forward 失败。HTTP 409。

    对应《GitHub 分布式协作协议》第 11 节 ``control push 冲突`` 与
    ``PR head 在审批后变化`` 两种场景。
    """

    error_code = ErrorCode.GIT_CONFLICT
    default_retryable = False


class GitEventRejectedError(DomainError):
    """``EventDecision`` 拒绝节点提交的 CoordinationEvent。

    ``reason_code`` 必须取自 ``ReasonCode.EVENT_*`` 枚举，禁止自由文本，
    以便节点按确定原因重试或转人工（详见《GitHub 分布式协作协议》第 6、11 节）。
    对应任务范围 TASK-021 ``record_event_decision`` 写入 ``EventDecision`` 时使用。
    """

    error_code = ErrorCode.GIT_EVENT_REJECTED
    default_retryable = False

    def __init__(
        self,
        message: str,
        *,
        reason_code: ReasonCode | str,
        context: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        if reason_code is None:
            raise ValueError("GitEventRejectedError 必须提供 reason_code")
        super().__init__(message, reason_code=reason_code, context=context, retryable=retryable)


# --------------------------------------------------------------------------- #
# 限流与外部依赖类
# --------------------------------------------------------------------------- #


class RateLimitedError(DomainError):
    """触发限流。HTTP 429。可重试。

    ``context`` 可携带 ``retry_after``（秒），API 层据此设置 ``Retry-After`` 响应头。
    """

    error_code = ErrorCode.RATE_LIMITED
    default_retryable = True


class BudgetExceededError(DomainError):
    """预算超限。HTTP 429。等待人工。"""

    error_code = ErrorCode.BUDGET_EXCEEDED
    default_retryable = False


class ExternalDependencyError(DomainError):
    """模型供应商、MCP、GitHub、本地 Git 等外部依赖失败。HTTP 502/503。

    ``context`` 可包含 ``provider``、``status_code``、``retry_after`` 等。
    ``retryable`` 默认 True，调用方按策略回退。对应系统设计 14.6 模型错误码
    与 25.15 ``AllModelCandidatesFailed`` 场景。
    """

    error_code = ErrorCode.EXTERNAL_DEPENDENCY_FAILED
    default_retryable = True


__all__ = [
    "ErrorCode",
    "ReasonCode",
    "DomainError",
    "ArgumentError",
    "ValidationError",
    "UnsupportedOperationError",
    "UnauthenticatedError",
    "PermissionDeniedError",
    "ToolPolicyDeniedError",
    "NotFoundError",
    "AlreadyExistsError",
    "VersionConflictError",
    "IdempotencyConflictError",
    "GitConflictError",
    "GitEventRejectedError",
    "RateLimitedError",
    "BudgetExceededError",
    "ExternalDependencyError",
]
