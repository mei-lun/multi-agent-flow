"""站内待办与人工决策契约（TASK-082）。

本模块定义 Inbox 人工决策服务对外暴露的 TypedDict 契约：

- ``InboxItemStatus``：待办状态字面量（PENDING/DECIDED/EXPIRED）；
- ``InboxItemType``：待办类型字面量（REVIEW_REQUEST/CHANGE_REQUEST/
  APPROVAL_REQUEST）；
- ``InboxDecision``：人工决策字面量（APPROVE/REJECT/REQUEST_CHANGES）；
- ``InboxPriority``：优先级字面量（LOW/NORMAL/HIGH/URGENT）；
- ``CreateInboxRequest``：``InboxService.create`` 请求体；
- ``DecideRequest``：``InboxService.decide`` 请求体；
- ``InboxItemView``：待办项对外视图，对应 ``inbox_items`` 表行。

设计原则（与 reviews/artifacts 模块一致）：
- 本文件只定义字段，不执行校验或业务逻辑；
- 字段名称属于已冻结接口，由 ``InboxServiceImpl`` 与 ``router`` 共享。
"""

from typing import Any, Literal, TypedDict

# --------------------------------------------------------------------------- #
# 枚举字面量
# --------------------------------------------------------------------------- #

#: 待办状态字面量。
#: - ``PENDING``：待处理，等待人工决策；
#: - ``DECIDED``：已决策（终态），由 ``decide`` 转入；
#: - ``EXPIRED``：已过期（终态），由 ``expire`` 转入。
InboxItemStatus = Literal["PENDING", "DECIDED", "EXPIRED"]

#: 待办类型字面量。
#: - ``REVIEW_REQUEST``：评审请求（关联 ``review_id``）；
#: - ``CHANGE_REQUEST``：变更请求；
#: - ``APPROVAL_REQUEST``：审批请求。
InboxItemType = Literal["REVIEW_REQUEST", "CHANGE_REQUEST", "APPROVAL_REQUEST"]

#: 人工决策字面量。
#: - ``APPROVE``：批准；
#: - ``REJECT``：拒绝；
#: - ``REQUEST_CHANGES``：请求修改。
InboxDecision = Literal["APPROVE", "REJECT", "REQUEST_CHANGES"]

#: 优先级字面量。
InboxPriority = Literal["LOW", "NORMAL", "HIGH", "URGENT"]


# --------------------------------------------------------------------------- #
# 请求与视图
# --------------------------------------------------------------------------- #


class CreateInboxRequest(TypedDict, total=False):
    """``InboxService.create`` 请求体。

    系统创建（如 QualityGate 评估后需要人工审批），不需权限检查。

    - ``project_id``：所属项目 ID（必填）；
    - ``title``：标题（必填）；
    - ``description``：描述（必填）；
    - ``item_type``：待办类型（必填）；
    - ``artifact_id``：关联 artifact ID（可选）；
    - ``review_id``：关联评审 ID（可选，``decide`` 时据此调用 ReviewService）；
    - ``assigned_to``：指定处理用户 ID（可选，未指定则所有 APPROVER 可见）；
    - ``priority``：优先级，默认 ``NORMAL``；
    - ``metadata``：附加元数据，默认 ``{}``。
    """

    project_id: str
    title: str
    description: str
    item_type: InboxItemType
    artifact_id: str | None
    review_id: str | None
    assigned_to: str | None
    priority: InboxPriority
    metadata: dict[str, Any]


class DecideRequest(TypedDict, total=False):
    """``InboxService.decide`` 请求体。

    - ``decision``：决策（APPROVE/REJECT/REQUEST_CHANGES，必填）；
    - ``comment``：决策评论（必填，人工决策必须说明理由）；
    - ``metadata``：附加元数据。
    """

    decision: InboxDecision
    comment: str
    metadata: dict[str, Any]


class InboxItemView(TypedDict):
    """``inbox_items`` 表行对外视图。

    - ``id``：待办项 ID（UUID）；
    - ``project_id``：所属项目 ID；
    - ``title`` / ``description``：标题与描述；
    - ``item_type``：待办类型；
    - ``artifact_id``：关联 artifact ID（可空）；
    - ``review_id``：关联评审 ID（可空）；
    - ``assigned_to``：指定处理用户 ID（可空，空表示所有 APPROVER 可见）；
    - ``priority``：优先级；
    - ``status``：状态（PENDING/DECIDED/EXPIRED）；
    - ``decision``：决策结果（APPROVE/REJECT/REQUEST_CHANGES，决策前为 None）；
    - ``decision_comment``：决策评论（决策前为 None）；
    - ``decided_by``：决策者 actor_id（决策前为 None）；
    - ``decided_at``：决策时间（ISO8601 带 UTC，决策前为 None）；
    - ``created_at``：创建时间（ISO8601 带 UTC）；
    - ``created_by``：创建者 actor_id；
    - ``version_no``：乐观锁版本号；
    - ``metadata``：附加元数据。
    """

    id: str
    project_id: str
    title: str
    description: str
    item_type: InboxItemType
    artifact_id: str | None
    review_id: str | None
    assigned_to: str | None
    priority: InboxPriority
    status: InboxItemStatus
    decision: InboxDecision | None
    decision_comment: str | None
    decided_by: str | None
    decided_at: str | None
    created_at: str
    created_by: str
    version_no: int
    metadata: dict[str, Any]


__all__ = [
    "CreateInboxRequest",
    "DecideRequest",
    "InboxDecision",
    "InboxItemStatus",
    "InboxItemType",
    "InboxItemView",
    "InboxPriority",
]
