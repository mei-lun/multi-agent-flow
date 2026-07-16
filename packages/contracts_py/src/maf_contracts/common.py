"""所有接口共同使用的基础传输类型。

本文件只定义字段，不执行校验或业务逻辑。实现阶段可把这些 ``TypedDict``
替换为等价的 Pydantic 模型，但字段名称和含义属于已冻结接口，不能随意改变。
"""

from typing import Any, Literal, NotRequired, TypedDict

JsonObject = dict[str, Any]


class ActorContext(TypedDict):
    """经过认证的调用者上下文，由认证中间件创建，客户端不得自行提交。"""

    user_id: str
    organization_id: str
    permission_keys: list[str]
    trace_id: str


class ExecutionContext(TypedDict):
    """节点本地根据 control assignment 和已发布角色构造的可信执行上下文。"""

    project_id: str
    task_id: str
    node_id: str
    assignment_id: str
    assignment_epoch: int
    based_on_control_commit: str
    role_version_id: str
    granted_model_policy_ids: list[str]
    granted_skill_version_ids: list[str]
    granted_tool_keys: list[str]
    granted_network_policy_id: str


class CommandMeta(TypedDict):
    """所有会改变状态的命令都应携带的控制信息。"""

    idempotency_key: str
    expected_version: NotRequired[int]
    reason: NotRequired[str]


class Money(TypedDict):
    """金额使用十进制字符串，禁止使用浮点数。"""

    amount: str
    currency: str


class PageQuery(TypedDict, total=False):
    """列表接口的公共查询字段。cursor 为空表示第一页。"""

    cursor: str
    limit: int
    sort: str
    direction: Literal["asc", "desc"]


class PageResult(TypedDict):
    """游标分页结果。items 的具体类型由各接口补充。"""

    items: list[JsonObject]
    next_cursor: str | None
    has_more: bool


class ErrorItem(TypedDict):
    """指向某个字段或规则的可操作错误。"""

    code: str
    message: str
    field: NotRequired[str]
    details: NotRequired[JsonObject]


class ErrorResponse(TypedDict):
    """所有 HTTP 和内部接口共用的失败响应。"""

    error_code: str
    message: str
    trace_id: str
    retryable: bool
    items: list[ErrorItem]


class VersionRef(TypedDict):
    """不可变配置版本引用。"""

    id: str
    version: int
    content_hash: str


class OperationResult(TypedDict):
    """不需要返回完整资源的命令结果。"""

    operation_id: str
    status: Literal["ACCEPTED", "COMPLETED", "REJECTED"]
    resource_id: NotRequired[str]
    resource_version: NotRequired[int]
