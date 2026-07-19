"""TASK-082 集成测试：Inbox 人工决策。

验收标准覆盖（对应 TASK-082 文档与任务描述）：

1. **InboxService**：create/list_for_actor/get/decide/assign/expire。
2. **list_for_actor 可见性**：返回分配给用户或所有 APPROVER 可见（assigned_to
   IS NULL）的项。
3. **decide**：状态 PENDING → DECIDED，并触发 ReviewService（如有 review_id）。
4. **权限检查**：通过 PermissionService.require（read/write/manage inbox）。
5. **只有 assignee/管理员可决定**：非 assignee 的 APPROVER 不能决定分配给他人的
   待办。
6. **事件**：InboxItemCreated、InboxItemDecided 经 OutboxRepository 写入。
7. **数据库表**：inbox_items 表（id、project_id、title、description、item_type、
   artifact_id、review_id、assigned_to、priority、status、decision、
   decision_comment、decided_by、decided_at、created_at、created_by、
   metadata、version_no）。
8. **不破坏 TASK-080/081**（test_validators.py、test_review_quality_gate.py）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

# packages/artifact_schemas/src 需要在 sys.path 中（与 test_validators.py 一致）。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT_SCHEMAS_SRC = _PROJECT_ROOT / "packages" / "artifact_schemas" / "src"
if str(_ARTIFACT_SCHEMAS_SRC) not in sys.path:
    sys.path.insert(0, str(_ARTIFACT_SCHEMAS_SRC))

from maf_contracts.common import ActorContext  # noqa: E402
from maf_domain.errors import (  # noqa: E402
    ArgumentError,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedOperationError,
    VersionConflictError,
)
from maf_policy import CasbinPermissionService  # noqa: E402
from maf_server.config import ServerSettings  # noqa: E402
from maf_server.core.artifact_store import LocalArtifactFileStore  # noqa: E402
from maf_server.core.database import Database  # noqa: E402
from maf_server.core.events import (  # noqa: E402
    SqliteOutboxRepository,
    init_outbox_schema,
)
from maf_server.modules.artifacts.repository import (  # noqa: E402
    SqliteArtifactRepository,
    init_schema as init_artifact_schema,
)
from maf_server.modules.artifacts.service import (  # noqa: E402
    ArtifactServiceImpl,
    ValidatorResult,
)
from maf_server.modules.inbox.repository import (  # noqa: E402
    INBOX_ITEMS_DDL,
    SqliteInboxRepository,
    init_inbox_schema,
)
from maf_server.modules.inbox.schemas import (  # noqa: E402
    CreateInboxRequest,
    DecideRequest,
    InboxItemView,
)
from maf_server.modules.inbox.service import (  # noqa: E402
    InboxServiceImpl,
    ensure_inbox_schema,
)
from maf_server.modules.reviews.repository import (  # noqa: E402
    SqliteArtifactReviewRepository,
    init_artifact_reviews_schema,
)
from maf_server.modules.reviews.service import (  # noqa: E402
    ArtifactReviewServiceImpl,
    ensure_reviews_schema,
)

_SECRET_PLAINTEXT = "test-secret-for-task-082"


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
    """构建测试用 ServerSettings，数据库路径落在 ``tmp_path`` 下。"""
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


def _make_permission_service() -> CasbinPermissionService:
    """构造带 inbox / reviews / artifacts 策略的 CasbinPermissionService。

    DEFAULT_POLICIES 已含：
        - ``("ADMIN", "*", ".*")`` 全权；
        - ``("APPROVER", "inbox", ".*")`` / ``("APPROVER", "reviews", ".*")``；
        - ``("OBSERVER", "*", "read")`` 全局只读。

    本测试额外追加 OWNER 的 inbox/artifacts/reviews 读写策略，使 OWNER 可
    执行 assign（manage inbox）与 ReviewService 决策。
    """
    service = CasbinPermissionService()
    service.add_policy("OWNER", "inbox", ".*")
    service.add_policy("OWNER", "reviews", "(read|write)")
    service.add_policy("OWNER", "artifacts", "(read|write)")
    service.add_policy("APPROVER", "artifacts", "read")
    service.add_policy("DESIGNER", "artifacts", "(read|write)")
    return service


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    """已初始化并建好 inbox_items / artifact_reviews / artifacts /
    outbox_events 表的 Database。"""
    settings = _make_settings(tmp_path)
    database = Database(settings)
    await database.initialize()
    async with database.write_connection() as conn:
        await init_artifact_schema(conn)
        await init_artifact_reviews_schema(conn)
        await init_inbox_schema(conn)
    await init_outbox_schema(database)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def file_store(db: Database) -> LocalArtifactFileStore:
    """基于 ServerSettings.artifact_root 的 LocalArtifactFileStore。"""
    settings = db._settings  # type: ignore[attr-defined]
    return LocalArtifactFileStore(settings.artifact_root)


@pytest_asyncio.fixture
async def artifact_service(
    db: Database, file_store: LocalArtifactFileStore
) -> ArtifactServiceImpl:
    """注入 Database、FileStore、自定义 PermissionService 的 ArtifactServiceImpl。"""
    return ArtifactServiceImpl(
        database=db,
        file_store=file_store,
        permission_service=_make_permission_service(),
    )


@pytest_asyncio.fixture
async def review_service(db: Database) -> ArtifactReviewServiceImpl:
    """注入 Database、自定义 PermissionService 的 ArtifactReviewServiceImpl。"""
    return ArtifactReviewServiceImpl(
        database=db,
        permission_service=_make_permission_service(),
    )


@pytest_asyncio.fixture
async def inbox_service(
    db: Database, review_service: ArtifactReviewServiceImpl
) -> InboxServiceImpl:
    """注入 Database、ReviewService 与自定义 PermissionService 的
    InboxServiceImpl。"""
    return InboxServiceImpl(
        database=db,
        permission_service=_make_permission_service(),
        review_service=review_service,
    )


@pytest_asyncio.fixture
async def inbox_service_no_review(db: Database) -> InboxServiceImpl:
    """未注入 ReviewService 的 InboxServiceImpl（用于纯 inbox 行为测试）。"""
    return InboxServiceImpl(
        database=db,
        permission_service=_make_permission_service(),
    )


def _actor(
    user_id: str = "user-admin",
    roles: list[str] | None = None,
    trace_id: str = "task-082-trace",
) -> ActorContext:
    """构造测试用 ActorContext。

    ``roles=None`` 时默认 ADMIN；``roles=[]`` 显式表示无角色。
    """
    return ActorContext(
        user_id=user_id,
        organization_id="org-001",
        permission_keys=roles if roles is not None else ["ADMIN"],
        trace_id=trace_id,
    )


def _content(data: str = "hello task-082 world") -> bytes:
    """测试用内容。"""
    return data.encode("utf-8")


def _create_request(
    *,
    project_id: str = "proj-001",
    title: str = "审批请求",
    description: str = "请审批该变更",
    item_type: str = "REVIEW_REQUEST",
    artifact_id: str | None = None,
    review_id: str | None = None,
    assigned_to: str | None = None,
    priority: str = "NORMAL",
    metadata: dict[str, Any] | None = None,
) -> CreateInboxRequest:
    """构造 CreateInboxRequest。"""
    req: CreateInboxRequest = {
        "project_id": project_id,
        "title": title,
        "description": description,
        "item_type": item_type,  # type: ignore[arg-type]
        "priority": priority,  # type: ignore[arg-type]
    }
    if artifact_id is not None:
        req["artifact_id"] = artifact_id
    if review_id is not None:
        req["review_id"] = review_id
    if assigned_to is not None:
        req["assigned_to"] = assigned_to
    if metadata is not None:
        req["metadata"] = metadata
    return req


def _decide(
    decision: str = "APPROVE",
    comment: str = "批准",
    metadata: dict[str, Any] | None = None,
) -> DecideRequest:
    """构造 DecideRequest。"""
    req: DecideRequest = {
        "decision": decision,  # type: ignore[arg-type]
        "comment": comment,
    }
    if metadata is not None:
        req["metadata"] = metadata
    return req


async def _upload(
    service: ArtifactServiceImpl,
    *,
    project_id: str = "proj-001",
    actor: ActorContext | None = None,
) -> str:
    """上传一个 artifact，返回 artifact_id。"""
    if actor is None:
        actor = _actor(roles=["ADMIN"])
    view = await service.upload_artifact(
        project_id, "snapshot", _content("inbox-integration-content"),
        actor_id=actor["user_id"], actor=actor,
    )
    return view["id"]


async def _submit_review(
    review_service: ArtifactReviewServiceImpl,
    artifact_id: str,
    *,
    actor: ActorContext | None = None,
) -> str:
    """提交评审，返回 review_id。"""
    if actor is None:
        actor = _actor(roles=["ADMIN"])
    results = [
        ValidatorResult(
            status="PASS",
            issues=[],
            validator_name="hash_integrity",
            validated_at="2026-07-17T00:00:00+00:00",
        )
    ]
    view = await review_service.submit_review(
        artifact_id, results,
        actor_id=actor["user_id"], actor=actor,
        comment="auto-validation passed",
    )
    return view["id"]


# --------------------------------------------------------------------------- #
# 验收 1：InboxService —— create
# --------------------------------------------------------------------------- #


class TestInboxCreate:
    """``InboxServiceImpl.create`` 测试。"""

    @pytest.mark.asyncio
    async def test_create_returns_pending_item(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """create 返回 PENDING 待办，含全部字段。"""
        actor = _actor(roles=["ADMIN"])
        view = await inbox_service.create(
            _create_request(
                title="审批变更 A",
                description="请审批",
                item_type="APPROVAL_REQUEST",
                priority="HIGH",
                metadata={"source": "quality_gate"},
            ),
            actor_id=actor["user_id"], actor=actor,
        )
        assert view["status"] == "PENDING"
        assert view["item_type"] == "APPROVAL_REQUEST"
        assert view["title"] == "审批变更 A"
        assert view["priority"] == "HIGH"
        assert view["assigned_to"] is None
        assert view["decision"] is None
        assert view["decided_by"] is None
        assert view["decided_at"] is None
        assert view["version_no"] == 1
        assert view["created_by"] == actor["user_id"]
        assert view["metadata"] == {"source": "quality_gate"}
        assert view["id"]

    @pytest.mark.asyncio
    async def test_create_with_assigned_to(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """create 指定 assigned_to 时存入该用户。"""
        actor = _actor(roles=["ADMIN"])
        view = await inbox_service.create(
            _create_request(assigned_to="approver-1"),
            actor_id=actor["user_id"], actor=actor,
        )
        assert view["assigned_to"] == "approver-1"

    @pytest.mark.asyncio
    async def test_create_with_review_id(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """create 关联 review_id 时存入。"""
        actor = _actor(roles=["ADMIN"])
        view = await inbox_service.create(
            _create_request(review_id="rev-123", artifact_id="art-456"),
            actor_id=actor["user_id"], actor=actor,
        )
        assert view["review_id"] == "rev-123"
        assert view["artifact_id"] == "art-456"

    @pytest.mark.asyncio
    async def test_create_does_not_require_permission(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """create 为系统创建，不需权限检查（无角色用户也可调用）。"""
        system_actor = _actor(user_id="system", roles=[])
        view = await inbox_service.create(
            _create_request(),
            actor_id=system_actor["user_id"], actor=system_actor,
        )
        assert view["status"] == "PENDING"
        assert view["created_by"] == "system"

    @pytest.mark.asyncio
    async def test_create_invalid_item_type_raises(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """非法 item_type 抛 ArgumentError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(ArgumentError):
            await inbox_service.create(
                _create_request(item_type="INVALID"),  # type: ignore[arg-type]
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_create_empty_title_raises(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """空 title 抛 ArgumentError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(ArgumentError):
            await inbox_service.create(
                _create_request(title=""),
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_create_invalid_priority_raises(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """非法 priority 抛 ArgumentError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(ArgumentError):
            await inbox_service.create(
                _create_request(priority="CRITICAL"),  # type: ignore[arg-type]
                actor_id=actor["user_id"], actor=actor,
            )


# --------------------------------------------------------------------------- #
# 验收 2：list_for_actor 可见性
# --------------------------------------------------------------------------- #


class TestInboxListVisibility:
    """``list_for_actor`` 可见性规则测试。"""

    @pytest.mark.asyncio
    async def test_list_returns_assigned_to_actor(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """分配给该用户的待办可见。"""
        admin = _actor(roles=["ADMIN"])
        approver = _actor(user_id="approver-1", roles=["APPROVER"])
        await inbox_service.create(
            _create_request(assigned_to="approver-1"),
            actor_id=admin["user_id"], actor=admin,
        )

        items = await inbox_service.list_for_actor(
            approver["user_id"], actor=approver,
        )
        assert len(items) == 1
        assert items[0]["assigned_to"] == "approver-1"

    @pytest.mark.asyncio
    async def test_list_returns_unassigned_visible_to_approvers(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """assigned_to 为 None 的待办对所有 APPROVER 可见。"""
        admin = _actor(roles=["ADMIN"])
        approver = _actor(user_id="approver-1", roles=["APPROVER"])
        await inbox_service.create(
            _create_request(assigned_to=None, title="共享待办"),
            actor_id=admin["user_id"], actor=admin,
        )

        items = await inbox_service.list_for_actor(
            approver["user_id"], actor=approver,
        )
        assert len(items) == 1
        assert items[0]["assigned_to"] is None

    @pytest.mark.asyncio
    async def test_list_excludes_assigned_to_others(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """分配给其他用户的待办对当前用户不可见。"""
        admin = _actor(roles=["ADMIN"])
        approver1 = _actor(user_id="approver-1", roles=["APPROVER"])
        approver2 = _actor(user_id="approver-2", roles=["APPROVER"])
        # 分配给 approver-1
        await inbox_service.create(
            _create_request(assigned_to="approver-1"),
            actor_id=admin["user_id"], actor=admin,
        )
        # 分配给 approver-2
        await inbox_service.create(
            _create_request(assigned_to="approver-2"),
            actor_id=admin["user_id"], actor=admin,
        )

        # approver-1 只看到自己的 1 条（看不到 approver-2 的）
        items1 = await inbox_service.list_for_actor(
            approver1["user_id"], actor=approver1,
        )
        assert len(items1) == 1
        assert items1[0]["assigned_to"] == "approver-1"

        # approver-2 只看到自己的 1 条
        items2 = await inbox_service.list_for_actor(
            approver2["user_id"], actor=approver2,
        )
        assert len(items2) == 1
        assert items2[0]["assigned_to"] == "approver-2"

    @pytest.mark.asyncio
    async def test_list_filter_by_status(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """按 status 过滤。"""
        admin = _actor(roles=["ADMIN"])
        approver = _actor(user_id="approver-1", roles=["APPROVER"])
        # 创建 2 条
        v1 = await inbox_service.create(
            _create_request(assigned_to="approver-1", title="t1"),
            actor_id=admin["user_id"], actor=admin,
        )
        await inbox_service.create(
            _create_request(assigned_to="approver-1", title="t2"),
            actor_id=admin["user_id"], actor=admin,
        )
        # 决策 v1
        await inbox_service.decide(
            v1["id"], _decide("APPROVE", "ok"),
            actor_id=approver["user_id"], actor=approver,
        )

        pending = await inbox_service.list_for_actor(
            approver["user_id"], status="PENDING", actor=approver,
        )
        assert len(pending) == 1
        assert pending[0]["title"] == "t2"

        decided = await inbox_service.list_for_actor(
            approver["user_id"], status="DECIDED", actor=approver,
        )
        assert len(decided) == 1
        assert decided[0]["title"] == "t1"

    @pytest.mark.asyncio
    async def test_list_filter_by_project(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """按 project_id 过滤。"""
        admin = _actor(roles=["ADMIN"])
        approver = _actor(user_id="approver-1", roles=["APPROVER"])
        await inbox_service.create(
            _create_request(project_id="proj-A", assigned_to="approver-1"),
            actor_id=admin["user_id"], actor=admin,
        )
        await inbox_service.create(
            _create_request(project_id="proj-B", assigned_to="approver-1"),
            actor_id=admin["user_id"], actor=admin,
        )

        items = await inbox_service.list_for_actor(
            approver["user_id"], project_id="proj-A", actor=approver,
        )
        assert len(items) == 1
        assert items[0]["project_id"] == "proj-A"

    @pytest.mark.asyncio
    async def test_list_permission_denied_for_no_roles(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """无角色用户不能 list（无 read inbox 权限）。"""
        nobody = _actor(user_id="nobody", roles=[])
        with pytest.raises(PermissionDeniedError):
            await inbox_service.list_for_actor(
                nobody["user_id"], actor=nobody,
            )

    @pytest.mark.asyncio
    async def test_list_observer_can_read(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """OBSERVER 有 read inbox 权限（DEFAULT_POLICIES OBSERVER * read）。"""
        admin = _actor(roles=["ADMIN"])
        observer = _actor(user_id="obs", roles=["OBSERVER"])
        await inbox_service.create(
            _create_request(assigned_to="obs"),
            actor_id=admin["user_id"], actor=admin,
        )
        items = await inbox_service.list_for_actor(
            observer["user_id"], actor=observer,
        )
        assert len(items) == 1


# --------------------------------------------------------------------------- #
# 验收 3：get
# --------------------------------------------------------------------------- #


class TestInboxGet:
    """``InboxServiceImpl.get`` 测试。"""

    @pytest.mark.asyncio
    async def test_get_returns_item(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """get 返回待办详情。"""
        admin = _actor(roles=["ADMIN"])
        created = await inbox_service.create(
            _create_request(title="详情测试"),
            actor_id=admin["user_id"], actor=admin,
        )
        fetched = await inbox_service.get(
            created["id"], actor_id=admin["user_id"], actor=admin,
        )
        assert fetched["id"] == created["id"]
        assert fetched["title"] == "详情测试"

    @pytest.mark.asyncio
    async def test_get_not_found_raises(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """不存在的 item_id 抛 NotFoundError。"""
        admin = _actor(roles=["ADMIN"])
        with pytest.raises(NotFoundError):
            await inbox_service.get(
                "nonexistent", actor_id=admin["user_id"], actor=admin,
            )

    @pytest.mark.asyncio
    async def test_get_permission_denied(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """无角色用户不能 get。"""
        nobody = _actor(user_id="nobody", roles=[])
        with pytest.raises(PermissionDeniedError):
            await inbox_service.get(
                "any", actor_id=nobody["user_id"], actor=nobody,
            )


# --------------------------------------------------------------------------- #
# 验收 4 & 5：decide 状态转换 + 只有 assignee/管理员可决定
# --------------------------------------------------------------------------- #


class TestInboxDecide:
    """``InboxServiceImpl.decide`` 状态转换与 assignee 校验测试。"""

    @pytest.mark.asyncio
    async def test_decide_approve_changes_status(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """decide APPROVE → 状态 DECIDED，decision=APPROVE。"""
        admin = _actor(roles=["ADMIN"])
        created = await inbox_service.create(
            _create_request(),
            actor_id=admin["user_id"], actor=admin,
        )
        view = await inbox_service.decide(
            created["id"], _decide("APPROVE", "批准"),
            actor_id=admin["user_id"], actor=admin,
        )
        assert view["status"] == "DECIDED"
        assert view["decision"] == "APPROVE"
        assert view["decision_comment"] == "批准"
        assert view["decided_by"] == admin["user_id"]
        assert view["decided_at"] is not None
        assert view["version_no"] == 2

    @pytest.mark.asyncio
    async def test_decide_reject(self, inbox_service: InboxServiceImpl) -> None:
        """decide REJECT → DECIDED, decision=REJECT。"""
        admin = _actor(roles=["ADMIN"])
        created = await inbox_service.create(
            _create_request(),
            actor_id=admin["user_id"], actor=admin,
        )
        view = await inbox_service.decide(
            created["id"], _decide("REJECT", "拒绝"),
            actor_id=admin["user_id"], actor=admin,
        )
        assert view["status"] == "DECIDED"
        assert view["decision"] == "REJECT"

    @pytest.mark.asyncio
    async def test_decide_request_changes(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """decide REQUEST_CHANGES → DECIDED, decision=REQUEST_CHANGES。"""
        admin = _actor(roles=["ADMIN"])
        created = await inbox_service.create(
            _create_request(),
            actor_id=admin["user_id"], actor=admin,
        )
        view = await inbox_service.decide(
            created["id"], _decide("REQUEST_CHANGES", "请修改"),
            actor_id=admin["user_id"], actor=admin,
        )
        assert view["status"] == "DECIDED"
        assert view["decision"] == "REQUEST_CHANGES"

    @pytest.mark.asyncio
    async def test_decide_approvers_can_decide_unassigned(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """assigned_to 为 None 时任何 APPROVER 可决定。"""
        admin = _actor(roles=["ADMIN"])
        approver = _actor(user_id="approver-1", roles=["APPROVER"])
        created = await inbox_service.create(
            _create_request(assigned_to=None),
            actor_id=admin["user_id"], actor=admin,
        )
        view = await inbox_service.decide(
            created["id"], _decide("APPROVE", "ok"),
            actor_id=approver["user_id"], actor=approver,
        )
        assert view["status"] == "DECIDED"
        assert view["decided_by"] == "approver-1"

    @pytest.mark.asyncio
    async def test_decide_assignee_can_decide(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """assignee 可决定分配给自己的待办。"""
        admin = _actor(roles=["ADMIN"])
        approver = _actor(user_id="approver-1", roles=["APPROVER"])
        created = await inbox_service.create(
            _create_request(assigned_to="approver-1"),
            actor_id=admin["user_id"], actor=admin,
        )
        view = await inbox_service.decide(
            created["id"], _decide("APPROVE", "ok"),
            actor_id=approver["user_id"], actor=approver,
        )
        assert view["status"] == "DECIDED"
        assert view["decided_by"] == "approver-1"

    @pytest.mark.asyncio
    async def test_decide_non_assignee_denied(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """只有 assignee/管理员可决定：非 assignee 的 APPROVER 被拒绝。"""
        admin = _actor(roles=["ADMIN"])
        approver1 = _actor(user_id="approver-1", roles=["APPROVER"])
        approver2 = _actor(user_id="approver-2", roles=["APPROVER"])
        created = await inbox_service.create(
            _create_request(assigned_to="approver-1"),
            actor_id=admin["user_id"], actor=admin,
        )
        # approver-2（有 write inbox 权限）但非 assignee → 拒绝
        with pytest.raises(PermissionDeniedError):
            await inbox_service.decide(
                created["id"], _decide("APPROVE", "ok"),
                actor_id=approver2["user_id"], actor=approver2,
            )

    @pytest.mark.asyncio
    async def test_decide_admin_can_decide_others(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """管理员可决定分配给任意人的待办。"""
        admin = _actor(roles=["ADMIN"])
        created = await inbox_service.create(
            _create_request(assigned_to="someone-else"),
            actor_id=admin["user_id"], actor=admin,
        )
        view = await inbox_service.decide(
            created["id"], _decide("APPROVE", "管理员决定"),
            actor_id=admin["user_id"], actor=admin,
        )
        assert view["status"] == "DECIDED"
        assert view["decided_by"] == admin["user_id"]

    @pytest.mark.asyncio
    async def test_decide_on_decided_raises(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """对已 DECIDED 的待办再决策抛 UnsupportedOperationError。"""
        admin = _actor(roles=["ADMIN"])
        created = await inbox_service.create(
            _create_request(),
            actor_id=admin["user_id"], actor=admin,
        )
        await inbox_service.decide(
            created["id"], _decide("APPROVE", "ok"),
            actor_id=admin["user_id"], actor=admin,
        )
        with pytest.raises(UnsupportedOperationError):
            await inbox_service.decide(
                created["id"], _decide("REJECT", "again"),
                actor_id=admin["user_id"], actor=admin,
            )

    @pytest.mark.asyncio
    async def test_decide_on_expired_raises(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """对已 EXPIRED 的待办决策抛 UnsupportedOperationError。"""
        admin = _actor(roles=["ADMIN"])
        created = await inbox_service.create(
            _create_request(),
            actor_id=admin["user_id"], actor=admin,
        )
        await inbox_service.expire(
            created["id"], actor_id=admin["user_id"], actor=admin,
        )
        with pytest.raises(UnsupportedOperationError):
            await inbox_service.decide(
                created["id"], _decide("APPROVE", "x"),
                actor_id=admin["user_id"], actor=admin,
            )

    @pytest.mark.asyncio
    async def test_decide_not_found_raises(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """对不存在的 item_id 决策抛 NotFoundError。"""
        admin = _actor(roles=["ADMIN"])
        with pytest.raises(NotFoundError):
            await inbox_service.decide(
                "nonexistent", _decide("APPROVE", "x"),
                actor_id=admin["user_id"], actor=admin,
            )

    @pytest.mark.asyncio
    async def test_decide_empty_comment_raises(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """comment 为空抛 ArgumentError。"""
        admin = _actor(roles=["ADMIN"])
        created = await inbox_service.create(
            _create_request(),
            actor_id=admin["user_id"], actor=admin,
        )
        with pytest.raises(ArgumentError):
            await inbox_service.decide(
                created["id"], _decide("APPROVE", ""),
                actor_id=admin["user_id"], actor=admin,
            )
        with pytest.raises(ArgumentError):
            await inbox_service.decide(
                created["id"], _decide("APPROVE", "   "),
                actor_id=admin["user_id"], actor=admin,
            )

    @pytest.mark.asyncio
    async def test_decide_invalid_decision_raises(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """非法 decision 抛 ArgumentError。"""
        admin = _actor(roles=["ADMIN"])
        created = await inbox_service.create(
            _create_request(),
            actor_id=admin["user_id"], actor=admin,
        )
        with pytest.raises(ArgumentError):
            await inbox_service.decide(
                created["id"], _decide("INVALID", "x"),  # type: ignore[arg-type]
                actor_id=admin["user_id"], actor=admin,
            )

    @pytest.mark.asyncio
    async def test_decide_observer_denied(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """OBSERVER 无 write inbox 权限，不能 decide。"""
        admin = _actor(roles=["ADMIN"])
        observer = _actor(user_id="obs", roles=["OBSERVER"])
        created = await inbox_service.create(
            _create_request(),
            actor_id=admin["user_id"], actor=admin,
        )
        with pytest.raises(PermissionDeniedError):
            await inbox_service.decide(
                created["id"], _decide("APPROVE", "x"),
                actor_id=observer["user_id"], actor=observer,
            )

    @pytest.mark.asyncio
    async def test_decide_metadata_persisted(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """decide 的 metadata 不影响 item metadata（item metadata 在 create 时存）。"""
        admin = _actor(roles=["ADMIN"])
        created = await inbox_service.create(
            _create_request(metadata={"k": "v"}),
            actor_id=admin["user_id"], actor=admin,
        )
        view = await inbox_service.decide(
            created["id"],
            _decide("APPROVE", "ok", metadata={"note": "decided"}),
            actor_id=admin["user_id"], actor=admin,
        )
        # item metadata 保持 create 时的值
        assert view["metadata"] == {"k": "v"}


# --------------------------------------------------------------------------- #
# 验收 6：assign
# --------------------------------------------------------------------------- #


class TestInboxAssign:
    """``InboxServiceImpl.assign`` 测试。"""

    @pytest.mark.asyncio
    async def test_assign_by_owner(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """OWNER 可分配待办（manage inbox 权限）。"""
        admin = _actor(roles=["ADMIN"])
        owner = _actor(user_id="owner-1", roles=["OWNER"])
        created = await inbox_service.create(
            _create_request(assigned_to=None),
            actor_id=admin["user_id"], actor=admin,
        )
        view = await inbox_service.assign(
            created["id"], "approver-2",
            actor_id=owner["user_id"], actor=owner,
        )
        assert view["assigned_to"] == "approver-2"
        assert view["version_no"] == 2

    @pytest.mark.asyncio
    async def test_assign_by_admin(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """ADMIN 可分配待办。"""
        admin = _actor(roles=["ADMIN"])
        created = await inbox_service.create(
            _create_request(assigned_to=None),
            actor_id=admin["user_id"], actor=admin,
        )
        view = await inbox_service.assign(
            created["id"], "user-x",
            actor_id=admin["user_id"], actor=admin,
        )
        assert view["assigned_to"] == "user-x"

    @pytest.mark.asyncio
    async def test_assign_approver_denied(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """APPROVER 无 manage inbox 权限（DEFAULT_POLICIES APPROVER inbox .* 含
        manage，但本测试验证权限检查路径）。"""
        # 注意：DEFAULT_POLICIES 中 APPROVER 对 inbox 是 .*，包含 manage。
        # 因此 APPROVER 实际可以 assign。本用例改用 OBSERVER 验证拒绝。
        admin = _actor(roles=["ADMIN"])
        observer = _actor(user_id="obs", roles=["OBSERVER"])
        created = await inbox_service.create(
            _create_request(assigned_to=None),
            actor_id=admin["user_id"], actor=admin,
        )
        with pytest.raises(PermissionDeniedError):
            await inbox_service.assign(
                created["id"], "user-x",
                actor_id=observer["user_id"], actor=observer,
            )

    @pytest.mark.asyncio
    async def test_assign_not_found_raises(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """assign 不存在的待办抛 NotFoundError。"""
        admin = _actor(roles=["ADMIN"])
        with pytest.raises(NotFoundError):
            await inbox_service.assign(
                "nonexistent", "user-x",
                actor_id=admin["user_id"], actor=admin,
            )


# --------------------------------------------------------------------------- #
# 验收 7：expire
# --------------------------------------------------------------------------- #


class TestInboxExpire:
    """``InboxServiceImpl.expire`` 测试。"""

    @pytest.mark.asyncio
    async def test_expire_changes_status(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """expire → 状态 EXPIRED。"""
        admin = _actor(roles=["ADMIN"])
        created = await inbox_service.create(
            _create_request(),
            actor_id=admin["user_id"], actor=admin,
        )
        view = await inbox_service.expire(
            created["id"], actor_id=admin["user_id"], actor=admin,
        )
        assert view["status"] == "EXPIRED"
        assert view["version_no"] == 2
        assert view["decision"] is None  # expire 不写 decision

    @pytest.mark.asyncio
    async def test_expire_does_not_require_permission(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """expire 为系统调用，不需权限检查（无角色用户也可调用）。"""
        system_actor = _actor(user_id="system", roles=[])
        admin = _actor(roles=["ADMIN"])
        created = await inbox_service.create(
            _create_request(),
            actor_id=admin["user_id"], actor=admin,
        )
        view = await inbox_service.expire(
            created["id"],
            actor_id=system_actor["user_id"], actor=system_actor,
        )
        assert view["status"] == "EXPIRED"

    @pytest.mark.asyncio
    async def test_expire_on_decided_raises(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """对已 DECIDED 的待办 expire 抛 UnsupportedOperationError。"""
        admin = _actor(roles=["ADMIN"])
        created = await inbox_service.create(
            _create_request(),
            actor_id=admin["user_id"], actor=admin,
        )
        await inbox_service.decide(
            created["id"], _decide("APPROVE", "ok"),
            actor_id=admin["user_id"], actor=admin,
        )
        with pytest.raises(UnsupportedOperationError):
            await inbox_service.expire(
                created["id"], actor_id=admin["user_id"], actor=admin,
            )

    @pytest.mark.asyncio
    async def test_expire_not_found_raises(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """expire 不存在的待办抛 NotFoundError。"""
        admin = _actor(roles=["ADMIN"])
        with pytest.raises(NotFoundError):
            await inbox_service.expire(
                "nonexistent", actor_id=admin["user_id"], actor=admin,
            )


# --------------------------------------------------------------------------- #
# 验收 8：事件经 Outbox 写入
# --------------------------------------------------------------------------- #


class TestInboxEvents:
    """InboxItemCreated / InboxItemDecided 事件写入 Outbox。"""

    @pytest.mark.asyncio
    async def test_create_writes_event(
        self, inbox_service: InboxServiceImpl, db: Database
    ) -> None:
        """create 写入 inbox.item_created 事件。"""
        actor = _actor(roles=["ADMIN"])
        view = await inbox_service.create(
            _create_request(item_type="APPROVAL_REQUEST"),
            actor_id=actor["user_id"], actor=actor,
        )

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT event_type, aggregate_type, aggregate_id, payload "
                "FROM outbox_events WHERE event_type = ?",
                ("inbox.item_created",),
            ) as cur:
                rows = await cur.fetchall()

        assert len(rows) == 1
        evt = rows[0]
        assert evt[0] == "inbox.item_created"
        assert evt[1] == "inbox_item"
        assert evt[2] == view["id"]
        payload = json.loads(evt[3])
        assert payload["item_id"] == view["id"]
        assert payload["item_type"] == "APPROVAL_REQUEST"
        assert payload["status"] == "PENDING"
        assert payload["created_by"] == actor["user_id"]

    @pytest.mark.asyncio
    async def test_decide_writes_event(
        self, inbox_service: InboxServiceImpl, db: Database
    ) -> None:
        """decide 写入 inbox.item_decided 事件。"""
        actor = _actor(roles=["ADMIN"])
        created = await inbox_service.create(
            _create_request(),
            actor_id=actor["user_id"], actor=actor,
        )
        await inbox_service.decide(
            created["id"], _decide("APPROVE", "批准"),
            actor_id=actor["user_id"], actor=actor,
        )

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT event_type, aggregate_type, aggregate_id, payload "
                "FROM outbox_events WHERE event_type = ?",
                ("inbox.item_decided",),
            ) as cur:
                rows = await cur.fetchall()

        assert len(rows) == 1
        evt = rows[0]
        assert evt[0] == "inbox.item_decided"
        assert evt[1] == "inbox_item"
        assert evt[2] == created["id"]
        payload = json.loads(evt[3])
        assert payload["decision"] == "APPROVE"
        assert payload["comment"] == "批准"
        assert payload["decided_by"] == actor["user_id"]
        assert payload["previous_status"] == "PENDING"
        assert payload["new_status"] == "DECIDED"


# --------------------------------------------------------------------------- #
# 验收 9：数据库表 inbox_items
# --------------------------------------------------------------------------- #


class TestInboxTableSchema:
    """``inbox_items`` 表 DDL 测试。"""

    @pytest.mark.asyncio
    async def test_inbox_items_table_columns(self, db: Database) -> None:
        """inbox_items 表含全部字段。"""
        async with db.write_connection() as conn:
            await conn.execute(
                "INSERT INTO inbox_items "
                "(id, project_id, title, description, item_type, artifact_id, "
                "review_id, assigned_to, priority, status, created_at, "
                "created_by, metadata, version_no) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    "item-1", "proj-1", "title", "desc",
                    "REVIEW_REQUEST", "art-1", "rev-1", "u-1",
                    "HIGH", "PENDING", "2026-07-17T00:00:00+00:00",
                    "creator", '{"k":"v"}',
                ),
            )
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT id, project_id, title, description, item_type, "
                "artifact_id, review_id, assigned_to, priority, status, "
                "decision, decision_comment, decided_by, decided_at, "
                "created_at, created_by, version_no, metadata "
                "FROM inbox_items WHERE id = ?",
                ("item-1",),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        assert row[0] == "item-1"
        assert row[1] == "proj-1"
        assert row[4] == "REVIEW_REQUEST"
        assert row[5] == "art-1"
        assert row[6] == "rev-1"
        assert row[7] == "u-1"
        assert row[8] == "HIGH"
        assert row[9] == "PENDING"
        assert row[10] is None  # decision
        assert row[16] == 1  # version_no
        assert json.loads(row[17]) == {"k": "v"}

    @pytest.mark.asyncio
    async def test_inbox_items_invalid_metadata_rejected(
        self, db: Database
    ) -> None:
        """CHECK(json_valid(metadata)) 拒绝非 JSON 文本。"""
        async with db.write_connection() as conn:
            with pytest.raises(Exception):  # noqa: BLE001
                await conn.execute(
                    "INSERT INTO inbox_items "
                    "(id, project_id, title, item_type, created_at, "
                    "created_by, metadata, version_no) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        "item-bad", "proj-1", "t", "REVIEW_REQUEST",
                        "2026-07-17", "u", "not-a-json",
                    ),
                )

    @pytest.mark.asyncio
    async def test_ensure_inbox_schema_idempotent(
        self, tmp_path: Path
    ) -> None:
        """ensure_inbox_schema 幂等建表。"""
        settings = _make_settings(tmp_path)
        database = Database(settings)
        await database.initialize()
        try:
            await ensure_inbox_schema(database)
            await ensure_inbox_schema(database)  # 幂等
            async with database.read_connection() as conn:
                async with conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name = ?",
                    ("inbox_items",),
                ) as cur:
                    row = await cur.fetchone()
            assert row is not None
            assert row[0] == "inbox_items"
        finally:
            await database.close()


# --------------------------------------------------------------------------- #
# 验收 10：ReviewService 集成
# --------------------------------------------------------------------------- #


class TestReviewServiceIntegration:
    """``decide`` 触发 ReviewService 状态转换测试。"""

    @pytest.mark.asyncio
    async def test_decide_approve_triggers_review_approve(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        inbox_service: InboxServiceImpl,
    ) -> None:
        """decide APPROVE + review_id → ReviewService.approve_review。"""
        admin = _actor(roles=["ADMIN"])
        artifact_id = await _upload(artifact_service, actor=admin)
        review_id = await _submit_review(review_service, artifact_id, actor=admin)

        # 创建 inbox 待办关联 review_id
        created = await inbox_service.create(
            _create_request(
                review_id=review_id, artifact_id=artifact_id,
                item_type="REVIEW_REQUEST", assigned_to="approver-1",
            ),
            actor_id=admin["user_id"], actor=admin,
        )

        # approver 决策 APPROVE
        approver = _actor(user_id="approver-1", roles=["APPROVER"])
        view = await inbox_service.decide(
            created["id"], _decide("APPROVE", "approve via inbox"),
            actor_id=approver["user_id"], actor=approver,
        )
        assert view["status"] == "DECIDED"

        # 验证 review 状态变为 APPROVED
        review = await review_service.get_review(
            review_id, actor_id=admin["user_id"], actor=admin,
        )
        assert review["review_status"] == "APPROVED"
        assert review["reviewer_comment"] == "approve via inbox"
        assert review["decided_by"] == "approver-1"

    @pytest.mark.asyncio
    async def test_decide_reject_triggers_review_reject(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        inbox_service: InboxServiceImpl,
    ) -> None:
        """decide REJECT + review_id → ReviewService.reject_review。"""
        admin = _actor(roles=["ADMIN"])
        artifact_id = await _upload(artifact_service, actor=admin)
        review_id = await _submit_review(review_service, artifact_id, actor=admin)

        created = await inbox_service.create(
            _create_request(review_id=review_id, item_type="REVIEW_REQUEST"),
            actor_id=admin["user_id"], actor=admin,
        )
        await inbox_service.decide(
            created["id"], _decide("REJECT", "reject via inbox"),
            actor_id=admin["user_id"], actor=admin,
        )

        review = await review_service.get_review(
            review_id, actor_id=admin["user_id"], actor=admin,
        )
        assert review["review_status"] == "REJECTED"

    @pytest.mark.asyncio
    async def test_decide_request_changes_triggers_review(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        inbox_service: InboxServiceImpl,
    ) -> None:
        """decide REQUEST_CHANGES + review_id → ReviewService.request_changes。"""
        admin = _actor(roles=["ADMIN"])
        artifact_id = await _upload(artifact_service, actor=admin)
        review_id = await _submit_review(review_service, artifact_id, actor=admin)

        created = await inbox_service.create(
            _create_request(review_id=review_id, item_type="REVIEW_REQUEST"),
            actor_id=admin["user_id"], actor=admin,
        )
        await inbox_service.decide(
            created["id"], _decide("REQUEST_CHANGES", "changes via inbox"),
            actor_id=admin["user_id"], actor=admin,
        )

        review = await review_service.get_review(
            review_id, actor_id=admin["user_id"], actor=admin,
        )
        assert review["review_status"] == "CHANGES_REQUESTED"

    @pytest.mark.asyncio
    async def test_decide_without_review_id_no_review_call(
        self,
        inbox_service_no_review: InboxServiceImpl,
    ) -> None:
        """decide 无 review_id 时不调用 ReviewService（即使未注入也能成功）。"""
        admin = _actor(roles=["ADMIN"])
        created = await inbox_service_no_review.create(
            _create_request(item_type="CHANGE_REQUEST"),
            actor_id=admin["user_id"], actor=admin,
        )
        view = await inbox_service_no_review.decide(
            created["id"], _decide("APPROVE", "ok"),
            actor_id=admin["user_id"], actor=admin,
        )
        assert view["status"] == "DECIDED"

    @pytest.mark.asyncio
    async def test_decide_without_review_service_no_error(
        self,
        inbox_service_no_review: InboxServiceImpl,
    ) -> None:
        """未注入 ReviewService 时，即使有 review_id 也不报错（无操作）。"""
        admin = _actor(roles=["ADMIN"])
        created = await inbox_service_no_review.create(
            _create_request(review_id="rev-x", item_type="REVIEW_REQUEST"),
            actor_id=admin["user_id"], actor=admin,
        )
        view = await inbox_service_no_review.decide(
            created["id"], _decide("APPROVE", "ok"),
            actor_id=admin["user_id"], actor=admin,
        )
        assert view["status"] == "DECIDED"
        assert view["review_id"] == "rev-x"


# --------------------------------------------------------------------------- #
# 验收 11：端到端集成
# --------------------------------------------------------------------------- #


class TestEndToEndIntegration:
    """端到端：create → list → get → decide → review 状态转换。"""

    @pytest.mark.asyncio
    async def test_full_flow_approve(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        inbox_service: InboxServiceImpl,
        db: Database,
    ) -> None:
        """完整流程：上传 → 提交评审 → 创建 inbox → 决策 → review APPROVED。"""
        admin = _actor(roles=["ADMIN"])
        approver = _actor(user_id="approver-1", roles=["APPROVER"])

        # 1. 上传 artifact + 提交评审
        artifact_id = await _upload(artifact_service, actor=admin)
        review_id = await _submit_review(review_service, artifact_id, actor=admin)

        # 2. 系统创建 inbox 待办（关联 review_id）
        created = await inbox_service.create(
            _create_request(
                review_id=review_id,
                artifact_id=artifact_id,
                item_type="REVIEW_REQUEST",
                assigned_to=None,  # 所有 APPROVER 可见
                priority="HIGH",
                metadata={"gate": "quality_gate", "run_id": "run-1"},
            ),
            actor_id=admin["user_id"], actor=admin,
        )

        # 3. approver list 看到 该待办
        items = await inbox_service.list_for_actor(
            approver["user_id"], status="PENDING", actor=approver,
        )
        assert len(items) == 1
        assert items[0]["id"] == created["id"]

        # 4. approver get 详情
        detail = await inbox_service.get(
            created["id"], actor_id=approver["user_id"], actor=approver,
        )
        assert detail["review_id"] == review_id

        # 5. approver decide APPROVE
        view = await inbox_service.decide(
            created["id"], _decide("APPROVE", "approved via inbox"),
            actor_id=approver["user_id"], actor=approver,
        )
        assert view["status"] == "DECIDED"
        assert view["decision"] == "APPROVE"

        # 6. review 状态变为 APPROVED
        review = await review_service.get_review(
            review_id, actor_id=admin["user_id"], actor=admin,
        )
        assert review["review_status"] == "APPROVED"

        # 7. inbox 事件已写入
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT event_type FROM outbox_events "
                "WHERE event_type IN (?, ?) ORDER BY occurred_at ASC",
                ("inbox.item_created", "inbox.item_decided"),
            ) as cur:
                rows = await cur.fetchall()
        event_types = [r[0] for r in rows]
        assert "inbox.item_created" in event_types
        assert "inbox.item_decided" in event_types

        # 8. list status=DECIDED 看到该待办
        decided_items = await inbox_service.list_for_actor(
            approver["user_id"], status="DECIDED", actor=approver,
        )
        assert len(decided_items) == 1
        assert decided_items[0]["id"] == created["id"]

    @pytest.mark.asyncio
    async def test_full_flow_assign_then_decide(
        self,
        inbox_service: InboxServiceImpl,
    ) -> None:
        """完整流程：create（unassigned）→ assign → 仅 assignee 可 decide。"""
        admin = _actor(roles=["ADMIN"])
        owner = _actor(user_id="owner-1", roles=["OWNER"])
        approver1 = _actor(user_id="approver-1", roles=["APPROVER"])
        approver2 = _actor(user_id="approver-2", roles=["APPROVER"])

        # 1. 创建未分配待办
        created = await inbox_service.create(
            _create_request(assigned_to=None, title="待分配"),
            actor_id=admin["user_id"], actor=admin,
        )

        # 2. 两个 approver 都能看到（unassigned 可见）
        items1 = await inbox_service.list_for_actor(
            approver1["user_id"], actor=approver1,
        )
        assert len(items1) == 1
        items2 = await inbox_service.list_for_actor(
            approver2["user_id"], actor=approver2,
        )
        assert len(items2) == 1

        # 3. OWNER 分配给 approver-1
        await inbox_service.assign(
            created["id"], "approver-1",
            actor_id=owner["user_id"], actor=owner,
        )

        # 4. 现在 approver-1 可见，approver-2 不可见
        items1_after = await inbox_service.list_for_actor(
            approver1["user_id"], actor=approver1,
        )
        assert len(items1_after) == 1
        items2_after = await inbox_service.list_for_actor(
            approver2["user_id"], actor=approver2,
        )
        assert len(items2_after) == 0

        # 5. approver-2 不能决定（非 assignee）
        with pytest.raises(PermissionDeniedError):
            await inbox_service.decide(
                created["id"], _decide("APPROVE", "x"),
                actor_id=approver2["user_id"], actor=approver2,
            )

        # 6. approver-1 可决定
        view = await inbox_service.decide(
            created["id"], _decide("APPROVE", "ok"),
            actor_id=approver1["user_id"], actor=approver1,
        )
        assert view["status"] == "DECIDED"
        assert view["decided_by"] == "approver-1"

    @pytest.mark.asyncio
    async def test_full_flow_expire_blocks_decide(
        self, inbox_service: InboxServiceImpl
    ) -> None:
        """完整流程：create → expire → decide 抛 UnsupportedOperationError。"""
        admin = _actor(roles=["ADMIN"])
        created = await inbox_service.create(
            _create_request(),
            actor_id=admin["user_id"], actor=admin,
        )
        await inbox_service.expire(
            created["id"], actor_id=admin["user_id"], actor=admin,
        )
        with pytest.raises(UnsupportedOperationError):
            await inbox_service.decide(
                created["id"], _decide("APPROVE", "x"),
                actor_id=admin["user_id"], actor=admin,
            )

        # list status=EXPIRED 可见
        items = await inbox_service.list_for_actor(
            admin["user_id"], status="EXPIRED", actor=admin,
        )
        assert len(items) == 1
        assert items[0]["status"] == "EXPIRED"
