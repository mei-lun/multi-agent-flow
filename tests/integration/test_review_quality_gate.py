"""TASK-081 集成测试：Review 与 QualityGate。

验收标准覆盖（对应 TASK-081 文档与任务描述）：

1. **ReviewService**：submit/get/list（含 comment 参数与 status 过滤）。
2. **QualityGateService**：evaluate（blocking 门禁）/ get_quality_gate / set_quality_gate。
3. **数据库表**：``quality_gates`` 表（id、run_id、node_id、gate_definitions TEXT JSON、
   created_by、created_at、version_no），增强 ``artifact_reviews`` 表
   （reviewer_comment、review_status 字段）。
4. **ReviewStatus 枚举**：PENDING/APPROVED/REJECTED/CHANGES_REQUESTED，状态流转
   正确。
5. **权限**：approve/reject/request_changes 需 ``write reviews``（APPROVER/ADMIN）；
   submit/get/list 需 ``read reviews``。
6. **事件**：ReviewApproved、ReviewRejected、ChangesRequested、QualityGateEvaluated
   经 OutboxRepository 写入。
7. **不破坏 TASK-080**（test_validators.py）与 TASK-079（test_artifact_lineage.py）。
8. **QualityGate 评估**：blocking 门禁失败时整体 passed=False；非阻断失败
   → CHANGES_REQUESTED；全通过 → APPROVED。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

# packages/artifact_schemas/src 需要在 sys.path 中（pyproject.toml pythonpath 未含），
# 与 tests/integration/test_validators.py 一致。必须在 maf_artifact_schemas 导入前。
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT_SCHEMAS_SRC = _PROJECT_ROOT / "packages" / "artifact_schemas" / "src"
if str(_ARTIFACT_SCHEMAS_SRC) not in sys.path:
    sys.path.insert(0, str(_ARTIFACT_SCHEMAS_SRC))

from maf_artifact_schemas.quality_gate import (  # noqa: E402
    KNOWN_REVIEW_STATUSES,
    KNOWN_VALIDATOR_STATUSES,
    validate_gate_definition,
    validate_gate_definitions,
    validate_gate_name,
    validate_required_status,
    validate_validator_name,
)
from maf_contracts.common import ActorContext  # noqa: E402
from maf_domain.errors import (  # noqa: E402
    ArgumentError,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedOperationError,
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
    ValidatorIssue,
    ValidatorResult,
)
from maf_server.modules.reviews.repository import (  # noqa: E402
    SqliteArtifactReviewRepository,
    SqliteQualityGateRepository,
    init_artifact_reviews_schema,
    init_quality_gates_schema,
)
from maf_server.modules.reviews.service import (  # noqa: E402
    ArtifactReviewServiceImpl,
    QualityGateServiceImpl,
    ensure_reviews_schema,
)
from maf_server.modules.reviews.schemas import (  # noqa: E402
    GateDefinition,
    QualityGateConfig,
    QualityGateResult,
    ReviewStatus,
)

_SECRET_PLAINTEXT = "test-secret-for-task-081"


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
    """构造带 artifact / artifact_schemas / reviews 策略的 CasbinPermissionService。

    DEFAULT_POLICIES 已含 ``("APPROVER", "reviews", ".*")`` 与
    ``("OBSERVER", "*", "read")``；本测试额外追加 OWNER/DESIGNER 的 reviews
    读写策略，与 test_validators.py 对齐。
    """
    service = CasbinPermissionService()
    service.add_policy("OWNER", "artifacts", "(read|write)")
    service.add_policy("DESIGNER", "artifacts", "(read|write)")
    service.add_policy("APPROVER", "artifacts", "read")
    service.add_policy("OWNER", "artifact_schemas", "(read|write)")
    service.add_policy("DESIGNER", "artifact_schemas", "(read|write)")
    service.add_policy("APPROVER", "artifact_schemas", "read")
    service.add_policy("OWNER", "reviews", "(read|write)")
    service.add_policy("DESIGNER", "reviews", "(read|write)")
    return service


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    """已初始化并建好 artifacts / artifact_reviews / quality_gates / outbox_events
    表的 Database。"""
    settings = _make_settings(tmp_path)
    database = Database(settings)
    await database.initialize()
    # 建 artifacts + artifact_reviews + quality_gates 表
    async with database.write_connection() as conn:
        await init_artifact_schema(conn)
        await init_artifact_reviews_schema(conn)
        await init_quality_gates_schema(conn)
    # 建 outbox_events 表
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
async def gate_service(
    db: Database, review_service: ArtifactReviewServiceImpl
) -> QualityGateServiceImpl:
    """注入 Database、review_service 与自定义 PermissionService 的
    QualityGateServiceImpl。"""
    return QualityGateServiceImpl(
        database=db,
        review_service=review_service,
        permission_service=_make_permission_service(),
    )


def _actor(
    user_id: str = "user-admin",
    roles: list[str] | None = None,
    trace_id: str = "task-081-trace",
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


def _content(data: str = "hello task-081 world") -> bytes:
    """测试用内容。"""
    return data.encode("utf-8")


async def _upload(
    service: ArtifactServiceImpl,
    *,
    project_id: str = "proj-001",
    artifact_type: str = "snapshot",
    content: bytes | None = None,
    actor: ActorContext | None = None,
) -> str:
    """上传一个 artifact，返回 artifact_id。"""
    if actor is None:
        actor = _actor(roles=["ADMIN"])
    if content is None:
        content = _content("default-task-081-content")
    view = await service.upload_artifact(
        project_id,
        artifact_type,
        content,
        actor_id=actor["user_id"],
        actor=actor,
    )
    return view["id"]


async def _submit_review(
    review_service: ArtifactReviewServiceImpl,
    artifact_id: str,
    *,
    results: list[ValidatorResult] | None = None,
    comment: str | None = None,
    actor: ActorContext | None = None,
) -> str:
    """提交评审，返回 review_id。"""
    if actor is None:
        actor = _actor(roles=["ADMIN"])
    if results is None:
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
        comment=comment,
    )
    return view["id"]


def _gate(
    name: str = "schema_validation",
    validator: str = "json_schema:task_payload:v1",
    required_status: str = "PASS",
    blocking: bool = True,
) -> GateDefinition:
    """构造 GateDefinition。"""
    return GateDefinition(
        name=name,
        validator=validator,
        required_status=required_status,  # type: ignore[arg-type]
        blocking=blocking,
    )


# --------------------------------------------------------------------------- #
# 验收 1：ReviewService —— submit/get/list（含 comment 与 status 过滤）
# --------------------------------------------------------------------------- #


class TestReviewServiceCrud:
    """``ArtifactReviewServiceImpl`` submit/get/list 测试（TASK-081 增强）。"""

    @pytest.mark.asyncio
    async def test_submit_review_with_comment(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """submit_review 含 comment → reviewer_comment 存储，review_status=PENDING。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("submit-with-comment")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

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
            comment="自动化校验通过",
        )
        assert view["review_status"] == "PENDING"
        assert view["reviewer_comment"] == "自动化校验通过"
        assert view["decided_by"] == actor["user_id"]
        assert view["decided_at"] is not None
        # status 字段保持 PASS/FAIL/ERROR（与 review_status 正交）
        assert view["status"] == "PASS"

    @pytest.mark.asyncio
    async def test_submit_review_without_comment(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """submit_review 不传 comment → reviewer_comment=None，review_status=PENDING。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("submit-no-comment")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        view = await review_service.submit_review(
            artifact_id, [],
            actor_id=actor["user_id"], actor=actor,
        )
        assert view["review_status"] == "PENDING"
        assert view["reviewer_comment"] is None

    @pytest.mark.asyncio
    async def test_get_review_returns_enhanced_fields(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """get_review 返回包含 review_status/reviewer_comment 的视图。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("get-enhanced")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        review_id = await _submit_review(
            review_service, artifact_id,
            comment="initial submission",
            actor=actor,
        )
        fetched = await review_service.get_review(
            review_id,
            actor_id=actor["user_id"], actor=actor,
        )
        assert fetched["id"] == review_id
        assert fetched["review_status"] == "PENDING"
        assert fetched["reviewer_comment"] == "initial submission"
        assert fetched["decided_by"] == actor["user_id"]

    @pytest.mark.asyncio
    async def test_list_reviews_filter_by_status(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """list_reviews 按 review_status 过滤。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("list-filter")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        # 提交 3 次评审，全部 PENDING
        for i in range(3):
            await _submit_review(
                review_service, artifact_id,
                comment=f"submission-{i}",
                actor=actor,
            )

        # 过滤 PENDING：返回 3 条
        pending = await review_service.list_reviews(
            artifact_id,
            actor_id=actor["user_id"], actor=actor,
            status="PENDING",
        )
        assert len(pending) == 3
        assert all(r["review_status"] == "PENDING" for r in pending)

        # 过滤 APPROVED：返回 0 条
        approved = await review_service.list_reviews(
            artifact_id,
            actor_id=actor["user_id"], actor=actor,
            status="APPROVED",
        )
        assert approved == []

        # 不过滤：返回 3 条
        all_reviews = await review_service.list_reviews(
            artifact_id,
            actor_id=actor["user_id"], actor=actor,
        )
        assert len(all_reviews) == 3

    @pytest.mark.asyncio
    async def test_list_reviews_invalid_status_raises(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """list_reviews 传入非法 status 抛 ArgumentError。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("list-invalid")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        with pytest.raises(ArgumentError):
            await review_service.list_reviews(
                artifact_id,
                actor_id=actor["user_id"], actor=actor,
                status="INVALID",  # type: ignore[arg-type]
            )


# --------------------------------------------------------------------------- #
# 验收 2：ReviewService —— approve/reject/request_changes 状态流转
# --------------------------------------------------------------------------- #


class TestReviewStatusTransition:
    """``approve_review``/``reject_review``/``request_changes`` 状态流转测试。"""

    @pytest.mark.asyncio
    async def test_approve_pending_review(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """PENDING → APPROVED。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("approve-pending")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        review_id = await _submit_review(
            review_service, artifact_id, actor=actor
        )

        view = await review_service.approve_review(
            review_id,
            actor_id=actor["user_id"], actor=actor,
            comment="LGTM",
        )
        assert view["review_status"] == "APPROVED"
        assert view["reviewer_comment"] == "LGTM"
        assert view["decided_by"] == actor["user_id"]
        assert view["decided_at"] is not None
        # version_no 递增（乐观锁更新）
        assert view["version_no"] >= 2

    @pytest.mark.asyncio
    async def test_reject_pending_review(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """PENDING → REJECTED。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("reject-pending")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        review_id = await _submit_review(
            review_service, artifact_id, actor=actor
        )

        view = await review_service.reject_review(
            review_id,
            actor_id=actor["user_id"], actor=actor,
            comment="Not acceptable",
        )
        assert view["review_status"] == "REJECTED"
        assert view["reviewer_comment"] == "Not acceptable"

    @pytest.mark.asyncio
    async def test_request_changes_pending_review(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """PENDING → CHANGES_REQUESTED。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("request-changes-pending")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        review_id = await _submit_review(
            review_service, artifact_id, actor=actor
        )

        view = await review_service.request_changes(
            review_id,
            actor_id=actor["user_id"], actor=actor,
            comment="Please fix the schema",
        )
        assert view["review_status"] == "CHANGES_REQUESTED"
        assert view["reviewer_comment"] == "Please fix the schema"

    @pytest.mark.asyncio
    async def test_approve_after_changes_requested(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """CHANGES_REQUESTED → APPROVED（返工后可再次批准）。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("approve-after-changes")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        review_id = await _submit_review(
            review_service, artifact_id, actor=actor
        )

        # 先请求修改
        await review_service.request_changes(
            review_id,
            actor_id=actor["user_id"], actor=actor,
            comment="Fix needed",
        )
        # 返工后批准
        view = await review_service.approve_review(
            review_id,
            actor_id=actor["user_id"], actor=actor,
            comment="Fixed, approved",
        )
        assert view["review_status"] == "APPROVED"

    @pytest.mark.asyncio
    async def test_reject_after_changes_requested(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """CHANGES_REQUESTED → REJECTED（返工后可拒绝）。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("reject-after-changes")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        review_id = await _submit_review(
            review_service, artifact_id, actor=actor
        )

        await review_service.request_changes(
            review_id,
            actor_id=actor["user_id"], actor=actor,
            comment="Fix needed",
        )
        view = await review_service.reject_review(
            review_id,
            actor_id=actor["user_id"], actor=actor,
            comment="Still broken",
        )
        assert view["review_status"] == "REJECTED"

    @pytest.mark.asyncio
    async def test_approved_is_terminal(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """APPROVED 为终态，不可再 approve/reject/request_changes。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("approved-terminal")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        review_id = await _submit_review(
            review_service, artifact_id, actor=actor
        )
        await review_service.approve_review(
            review_id,
            actor_id=actor["user_id"], actor=actor,
            comment="OK",
        )

        # 不能再次 approve
        with pytest.raises(UnsupportedOperationError):
            await review_service.approve_review(
                review_id,
                actor_id=actor["user_id"], actor=actor,
                comment="again",
            )
        # 不能 reject
        with pytest.raises(UnsupportedOperationError):
            await review_service.reject_review(
                review_id,
                actor_id=actor["user_id"], actor=actor,
                comment="again",
            )
        # 不能 request_changes
        with pytest.raises(UnsupportedOperationError):
            await review_service.request_changes(
                review_id,
                actor_id=actor["user_id"], actor=actor,
                comment="again",
            )

    @pytest.mark.asyncio
    async def test_rejected_is_terminal(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """REJECTED 为终态，不可再 approve/reject/request_changes。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("rejected-terminal")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        review_id = await _submit_review(
            review_service, artifact_id, actor=actor
        )
        await review_service.reject_review(
            review_id,
            actor_id=actor["user_id"], actor=actor,
            comment="No",
        )

        with pytest.raises(UnsupportedOperationError):
            await review_service.approve_review(
                review_id,
                actor_id=actor["user_id"], actor=actor,
                comment="again",
            )
        with pytest.raises(UnsupportedOperationError):
            await review_service.request_changes(
                review_id,
                actor_id=actor["user_id"], actor=actor,
                comment="again",
            )

    @pytest.mark.asyncio
    async def test_request_changes_after_changes_requested_rejected(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """CHANGES_REQUESTED 不能再 request_changes（需先重新 submit）。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("changes-then-changes")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        review_id = await _submit_review(
            review_service, artifact_id, actor=actor
        )
        await review_service.request_changes(
            review_id,
            actor_id=actor["user_id"], actor=actor,
            comment="Fix 1",
        )
        with pytest.raises(UnsupportedOperationError):
            await review_service.request_changes(
                review_id,
                actor_id=actor["user_id"], actor=actor,
                comment="Fix 2",
            )

    @pytest.mark.asyncio
    async def test_decision_requires_comment(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """approve/reject/request_changes 必须提供 comment。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("decision-needs-comment")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        review_id = await _submit_review(
            review_service, artifact_id, actor=actor
        )

        with pytest.raises(ArgumentError):
            await review_service.approve_review(
                review_id,
                actor_id=actor["user_id"], actor=actor,
                comment="",
            )
        with pytest.raises(ArgumentError):
            await review_service.approve_review(
                review_id,
                actor_id=actor["user_id"], actor=actor,
                comment="   ",  # 仅空白
            )

    @pytest.mark.asyncio
    async def test_decision_not_found(
        self, review_service: ArtifactReviewServiceImpl
    ) -> None:
        """对不存在的 review_id 决策抛 NotFoundError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(NotFoundError):
            await review_service.approve_review(
                "nonexistent",
                actor_id=actor["user_id"], actor=actor,
                comment="x",
            )


# --------------------------------------------------------------------------- #
# 验收 3：权限检查 —— write reviews vs read reviews
# --------------------------------------------------------------------------- #


class TestReviewPermissions:
    """``approve/reject/request_changes`` 需 write reviews；submit/get/list 需 read。"""

    @pytest.mark.asyncio
    async def test_observer_can_read_but_not_decide(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """OBSERVER 可 get/list reviews，但不能 approve/reject/request_changes。"""
        admin = _actor(roles=["ADMIN"])
        observer = _actor(user_id="obs", roles=["OBSERVER"])
        content = _content("observer-perm")
        artifact_id = await _upload(
            artifact_service, content=content, actor=admin
        )
        review_id = await _submit_review(
            review_service, artifact_id, actor=admin
        )

        # observer 可 get
        fetched = await review_service.get_review(
            review_id,
            actor_id=observer["user_id"], actor=observer,
        )
        assert fetched["id"] == review_id

        # observer 可 list
        listed = await review_service.list_reviews(
            artifact_id,
            actor_id=observer["user_id"], actor=observer,
        )
        assert len(listed) == 1

        # observer 不能 approve（无 write reviews）
        with pytest.raises(PermissionDeniedError):
            await review_service.approve_review(
                review_id,
                actor_id=observer["user_id"], actor=observer,
                comment="ok",
            )
        # observer 不能 reject
        with pytest.raises(PermissionDeniedError):
            await review_service.reject_review(
                review_id,
                actor_id=observer["user_id"], actor=observer,
                comment="no",
            )
        # observer 不能 request_changes
        with pytest.raises(PermissionDeniedError):
            await review_service.request_changes(
                review_id,
                actor_id=observer["user_id"], actor=observer,
                comment="change",
            )

    @pytest.mark.asyncio
    async def test_approver_can_decide(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """APPROVER 可 approve/reject/request_changes（DEFAULT_POLICIES 含 APPROVER reviews .*）。"""
        admin = _actor(roles=["ADMIN"])
        approver = _actor(user_id="approver", roles=["APPROVER"])
        content = _content("approver-perm")
        artifact_id = await _upload(
            artifact_service, content=content, actor=admin
        )
        review_id = await _submit_review(
            review_service, artifact_id, actor=approver
        )

        view = await review_service.approve_review(
            review_id,
            actor_id=approver["user_id"], actor=approver,
            comment="approved by approver",
        )
        assert view["review_status"] == "APPROVED"
        assert view["decided_by"] == approver["user_id"]

    @pytest.mark.asyncio
    async def test_owner_can_decide(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
    ) -> None:
        """OWNER 可 approve/reject/request_changes（fixture 追加 OWNER reviews 策略）。"""
        admin = _actor(roles=["ADMIN"])
        owner = _actor(user_id="owner", roles=["OWNER"])
        content = _content("owner-perm")
        artifact_id = await _upload(
            artifact_service, content=content, actor=admin
        )
        review_id = await _submit_review(
            review_service, artifact_id, actor=owner
        )

        view = await review_service.reject_review(
            review_id,
            actor_id=owner["user_id"], actor=owner,
            comment="rejected by owner",
        )
        assert view["review_status"] == "REJECTED"

    @pytest.mark.asyncio
    async def test_no_roles_denied_all(
        self, review_service: ArtifactReviewServiceImpl
    ) -> None:
        """无角色用户被拒绝所有 review 操作。"""
        nobody = _actor(user_id="nobody", roles=[])
        with pytest.raises(PermissionDeniedError):
            await review_service.get_review(
                "any", actor_id=nobody["user_id"], actor=nobody,
            )
        with pytest.raises(PermissionDeniedError):
            await review_service.approve_review(
                "any",
                actor_id=nobody["user_id"], actor=nobody,
                comment="x",
            )


# --------------------------------------------------------------------------- #
# 验收 4：事件经 Outbox 写入
# --------------------------------------------------------------------------- #


class TestReviewEvents:
    """Review 决策与 QualityGate 评估事件写入 Outbox。"""

    @pytest.mark.asyncio
    async def test_approve_writes_event(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        db: Database,
    ) -> None:
        """approve_review 写入 review.approved 事件。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("event-approve")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        review_id = await _submit_review(
            review_service, artifact_id, actor=actor
        )

        await review_service.approve_review(
            review_id,
            actor_id=actor["user_id"], actor=actor,
            comment="approved",
        )

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT event_type, aggregate_type, aggregate_id, payload "
                "FROM outbox_events WHERE event_type = ? "
                "ORDER BY occurred_at ASC",
                ("review.approved",),
            ) as cur:
                rows = await cur.fetchall()

        assert len(rows) == 1
        evt = rows[0]
        assert evt[0] == "review.approved"
        assert evt[1] == "artifact_review"
        assert evt[2] == review_id
        payload = json.loads(evt[3])
        assert payload["review_id"] == review_id
        assert payload["new_status"] == "APPROVED"
        assert payload["comment"] == "approved"
        assert payload["decided_by"] == actor["user_id"]

    @pytest.mark.asyncio
    async def test_reject_writes_event(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        db: Database,
    ) -> None:
        """reject_review 写入 review.rejected 事件。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("event-reject")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        review_id = await _submit_review(
            review_service, artifact_id, actor=actor
        )

        await review_service.reject_review(
            review_id,
            actor_id=actor["user_id"], actor=actor,
            comment="rejected",
        )

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT event_type FROM outbox_events "
                "WHERE event_type = ?",
                ("review.rejected",),
            ) as cur:
                rows = await cur.fetchall()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_request_changes_writes_event(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        db: Database,
    ) -> None:
        """request_changes 写入 review.changes_requested 事件。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("event-changes")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        review_id = await _submit_review(
            review_service, artifact_id, actor=actor
        )

        await review_service.request_changes(
            review_id,
            actor_id=actor["user_id"], actor=actor,
            comment="change requested",
        )

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT event_type FROM outbox_events "
                "WHERE event_type = ?",
                ("review.changes_requested",),
            ) as cur:
                rows = await cur.fetchall()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_submit_review_writes_event_with_review_status(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        db: Database,
    ) -> None:
        """submit_review 写入的事件 payload 含 review_status=PENDING。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("event-submit")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        await review_service.submit_review(
            artifact_id, [],
            actor_id=actor["user_id"], actor=actor,
            comment="submit with comment",
        )

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT payload FROM outbox_events "
                "WHERE event_type = ?",
                ("artifact.review_submitted",),
            ) as cur:
                rows = await cur.fetchall()
        assert len(rows) == 1
        payload = json.loads(rows[0][0])
        assert payload["review_status"] == "PENDING"


# --------------------------------------------------------------------------- #
# 验收 5：QualityGateService —— evaluate
# --------------------------------------------------------------------------- #


class TestQualityGateEvaluate:
    """``QualityGateServiceImpl.evaluate`` 测试。"""

    @pytest.mark.asyncio
    async def test_evaluate_all_pass(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        gate_service: QualityGateServiceImpl,
    ) -> None:
        """所有 blocking 门禁通过 → passed=True, overall_status=APPROVED。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("gate-all-pass")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        # 提交评审：两个 Validator 都 PASS
        results = [
            ValidatorResult(
                status="PASS",
                issues=[],
                validator_name="hash_integrity",
                validated_at="2026-07-17T00:00:00+00:00",
            ),
            ValidatorResult(
                status="PASS",
                issues=[],
                validator_name="size_limit",
                validated_at="2026-07-17T00:00:00+00:00",
            ),
        ]
        await _submit_review(
            review_service, artifact_id,
            results=results, actor=actor,
        )

        gates = [
            _gate(name="hash", validator="hash_integrity",
                  required_status="PASS", blocking=True),
            _gate(name="size", validator="size_limit",
                  required_status="PASS", blocking=True),
        ]
        result = await gate_service.evaluate(
            artifact_id,
            gate_definitions=gates,
            actor_id=actor["user_id"], actor=actor,
        )

        assert result["passed"] is True
        assert result["overall_status"] == "APPROVED"
        assert len(result["gate_results"]) == 2
        assert all(g["passed"] for g in result["gate_results"])
        assert all(g["reason"] is None for g in result["gate_results"])

    @pytest.mark.asyncio
    async def test_evaluate_blocking_failure(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        gate_service: QualityGateServiceImpl,
    ) -> None:
        """blocking 门禁失败 → passed=False, overall_status=REJECTED。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("gate-blocking-fail")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        # hash PASS, size FAIL
        results = [
            ValidatorResult(
                status="PASS",
                issues=[],
                validator_name="hash_integrity",
                validated_at="2026-07-17T00:00:00+00:00",
            ),
            ValidatorResult(
                status="FAIL",
                issues=[
                    ValidatorIssue(
                        severity="ERROR",
                        code="size.exceeded",
                        message="too big",
                        path=None,
                    )
                ],
                validator_name="size_limit",
                validated_at="2026-07-17T00:00:00+00:00",
            ),
        ]
        await _submit_review(
            review_service, artifact_id,
            results=results, actor=actor,
        )

        gates = [
            _gate(name="hash", validator="hash_integrity",
                  required_status="PASS", blocking=True),
            _gate(name="size", validator="size_limit",
                  required_status="PASS", blocking=True),  # blocking 失败
        ]
        result = await gate_service.evaluate(
            artifact_id,
            gate_definitions=gates,
            actor_id=actor["user_id"], actor=actor,
        )

        assert result["passed"] is False
        assert result["overall_status"] == "REJECTED"
        # blocking 门禁失败必须被记录
        size_gate = [g for g in result["gate_results"]
                     if g["name"] == "size"][0]
        assert size_gate["passed"] is False
        assert size_gate["blocking"] is True
        assert size_gate["reason"] == "status_mismatch"
        assert size_gate["actual_status"] == "FAIL"

    @pytest.mark.asyncio
    async def test_evaluate_non_blocking_failure(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        gate_service: QualityGateServiceImpl,
    ) -> None:
        """非阻断门禁失败 → passed=True, overall_status=CHANGES_REQUESTED。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("gate-non-blocking-fail")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        results = [
            ValidatorResult(
                status="PASS",
                issues=[],
                validator_name="hash_integrity",
                validated_at="2026-07-17T00:00:00+00:00",
            ),
            ValidatorResult(
                status="FAIL",
                issues=[
                    ValidatorIssue(
                        severity="WARNING",
                        code="lint.warning",
                        message="style issue",
                        path=None,
                    )
                ],
                validator_name="lint",
                validated_at="2026-07-17T00:00:00+00:00",
            ),
        ]
        await _submit_review(
            review_service, artifact_id,
            results=results, actor=actor,
        )

        gates = [
            _gate(name="hash", validator="hash_integrity",
                  required_status="PASS", blocking=True),
            _gate(name="lint", validator="lint",
                  required_status="PASS", blocking=False),  # 非阻断
        ]
        result = await gate_service.evaluate(
            artifact_id,
            gate_definitions=gates,
            actor_id=actor["user_id"], actor=actor,
        )

        # blocking 全通过 → passed=True
        assert result["passed"] is True
        # 但有非阻断失败 → CHANGES_REQUESTED
        assert result["overall_status"] == "CHANGES_REQUESTED"

    @pytest.mark.asyncio
    async def test_evaluate_validator_missing(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        gate_service: QualityGateServiceImpl,
    ) -> None:
        """gate 引用的 validator 在评审结果中缺失 → 该门禁失败。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("gate-validator-missing")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        results = [
            ValidatorResult(
                status="PASS",
                issues=[],
                validator_name="hash_integrity",
                validated_at="2026-07-17T00:00:00+00:00",
            ),
        ]
        await _submit_review(
            review_service, artifact_id,
            results=results, actor=actor,
        )

        # 引用不存在的 validator
        gates = [
            _gate(name="missing", validator="nonexistent_validator",
                  required_status="PASS", blocking=True),
        ]
        result = await gate_service.evaluate(
            artifact_id,
            gate_definitions=gates,
            actor_id=actor["user_id"], actor=actor,
        )

        assert result["passed"] is False
        assert result["overall_status"] == "REJECTED"
        gate_result = result["gate_results"][0]
        assert gate_result["passed"] is False
        assert gate_result["reason"] == "validator_missing"
        assert gate_result["actual_status"] is None

    @pytest.mark.asyncio
    async def test_evaluate_no_review_raises(
        self,
        artifact_service: ArtifactServiceImpl,
        gate_service: QualityGateServiceImpl,
    ) -> None:
        """artifact 无评审记录 → NotFoundError。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("gate-no-review")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        with pytest.raises(NotFoundError):
            await gate_service.evaluate(
                artifact_id,
                gate_definitions=[_gate()],
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_evaluate_invalid_gates_raises(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        gate_service: QualityGateServiceImpl,
    ) -> None:
        """gate_definitions 非法 → ArgumentError。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("gate-invalid")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        await _submit_review(review_service, artifact_id, actor=actor)

        # 空列表
        with pytest.raises(ArgumentError):
            await gate_service.evaluate(
                artifact_id,
                gate_definitions=[],
                actor_id=actor["user_id"], actor=actor,
            )

        # 重复 name
        with pytest.raises(ArgumentError):
            await gate_service.evaluate(
                artifact_id,
                gate_definitions=[
                    _gate(name="dup"),
                    _gate(name="dup"),
                ],
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_evaluate_deterministic(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        gate_service: QualityGateServiceImpl,
    ) -> None:
        """相同 (评审结果, gate_definitions) 多次评估结果一致。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("gate-deterministic")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        results = [
            ValidatorResult(
                status="FAIL",
                issues=[
                    ValidatorIssue(
                        severity="ERROR",
                        code="size.exceeded",
                        message="too big",
                        path=None,
                    )
                ],
                validator_name="size_limit",
                validated_at="2026-07-17T00:00:00+00:00",
            ),
        ]
        await _submit_review(
            review_service, artifact_id,
            results=results, actor=actor,
        )

        gates = [_gate(name="size", validator="size_limit",
                       required_status="PASS", blocking=True)]
        r1 = await gate_service.evaluate(
            artifact_id, gate_definitions=gates,
            actor_id=actor["user_id"], actor=actor,
        )
        r2 = await gate_service.evaluate(
            artifact_id, gate_definitions=gates,
            actor_id=actor["user_id"], actor=actor,
        )
        # 确定性：passed/overall_status/gate_results 一致（evaluated_at 可能不同）
        assert r1["passed"] == r2["passed"]
        assert r1["overall_status"] == r2["overall_status"]
        assert (
            [g["passed"] for g in r1["gate_results"]]
            == [g["passed"] for g in r2["gate_results"]]
        )
        assert (
            [g["reason"] for g in r1["gate_results"]]
            == [g["reason"] for g in r2["gate_results"]]
        )

    @pytest.mark.asyncio
    async def test_evaluate_writes_event(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        gate_service: QualityGateServiceImpl,
        db: Database,
    ) -> None:
        """evaluate 写入 quality_gate.evaluated 事件。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("gate-event")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )
        await _submit_review(review_service, artifact_id, actor=actor)

        gates = [_gate(name="hash", validator="hash_integrity")]
        await gate_service.evaluate(
            artifact_id, gate_definitions=gates,
            actor_id=actor["user_id"], actor=actor,
        )

        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT event_type, aggregate_type, aggregate_id, payload "
                "FROM outbox_events WHERE event_type = ?",
                ("quality_gate.evaluated",),
            ) as cur:
                rows = await cur.fetchall()

        assert len(rows) == 1
        evt = rows[0]
        assert evt[0] == "quality_gate.evaluated"
        assert evt[1] == "quality_gate"
        assert evt[2] == artifact_id
        payload = json.loads(evt[3])
        assert payload["artifact_id"] == artifact_id
        assert "passed" in payload
        assert "overall_status" in payload
        assert "gate_count" in payload


# --------------------------------------------------------------------------- #
# 验收 6：QualityGateService —— get/set
# --------------------------------------------------------------------------- #


class TestQualityGateConfig:
    """``get_quality_gate`` / ``set_quality_gate`` 测试。"""

    @pytest.mark.asyncio
    async def test_set_and_get_quality_gate(
        self, gate_service: QualityGateServiceImpl
    ) -> None:
        """set 后 get 返回相同配置。"""
        actor = _actor(roles=["ADMIN"])
        gates = [
            _gate(name="hash", validator="hash_integrity",
                  required_status="PASS", blocking=True),
            _gate(name="size", validator="size_limit",
                  required_status="PASS", blocking=False),
        ]

        cfg = await gate_service.set_quality_gate(
            "run-001", gates,
            actor_id=actor["user_id"], actor=actor,
        )
        assert cfg["run_id"] == "run-001"
        assert cfg["node_id"] is None
        assert len(cfg["gate_definitions"]) == 2
        assert cfg["created_by"] == actor["user_id"]
        assert cfg["version_no"] == 1

        # get 验证
        fetched = await gate_service.get_quality_gate(
            "run-001",
            actor_id=actor["user_id"], actor=actor,
        )
        assert fetched["id"] == cfg["id"]
        assert fetched["run_id"] == "run-001"
        assert len(fetched["gate_definitions"]) == 2

    @pytest.mark.asyncio
    async def test_set_quality_gate_with_node_id(
        self, gate_service: QualityGateServiceImpl
    ) -> None:
        """set 可指定 node_id（Node 级别门禁）。"""
        actor = _actor(roles=["ADMIN"])
        gates = [_gate(name="hash", validator="hash_integrity")]

        cfg = await gate_service.set_quality_gate(
            "run-002", gates,
            actor_id=actor["user_id"], actor=actor,
            node_id="node-A",
        )
        assert cfg["run_id"] == "run-002"
        assert cfg["node_id"] == "node-A"

        fetched = await gate_service.get_quality_gate(
            "run-002", node_id="node-A",
            actor_id=actor["user_id"], actor=actor,
        )
        assert fetched["id"] == cfg["id"]
        assert fetched["node_id"] == "node-A"

    @pytest.mark.asyncio
    async def test_set_quality_gate_overwrites(
        self, gate_service: QualityGateServiceImpl
    ) -> None:
        """set 覆盖旧配置（同 run_id+node_id 唯一）。"""
        actor = _actor(roles=["ADMIN"])
        gates_v1 = [_gate(name="hash", validator="hash_integrity")]
        gates_v2 = [
            _gate(name="hash", validator="hash_integrity"),
            _gate(name="size", validator="size_limit",
                  required_status="PASS", blocking=False),
        ]

        cfg1 = await gate_service.set_quality_gate(
            "run-003", gates_v1,
            actor_id=actor["user_id"], actor=actor,
        )
        cfg2 = await gate_service.set_quality_gate(
            "run-003", gates_v2,
            actor_id=actor["user_id"], actor=actor,
        )

        # 第二次 set 覆盖第一次，config_id 不同
        assert cfg1["id"] != cfg2["id"]
        assert cfg2["run_id"] == "run-003"
        assert len(cfg2["gate_definitions"]) == 2

        # get 只返回最新配置
        fetched = await gate_service.get_quality_gate(
            "run-003",
            actor_id=actor["user_id"], actor=actor,
        )
        assert fetched["id"] == cfg2["id"]
        assert len(fetched["gate_definitions"]) == 2

    @pytest.mark.asyncio
    async def test_get_quality_gate_not_found(
        self, gate_service: QualityGateServiceImpl
    ) -> None:
        """get 不存在的配置 → NotFoundError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(NotFoundError):
            await gate_service.get_quality_gate(
                "nonexistent-run",
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_set_quality_gate_invalid_gates_raises(
        self, gate_service: QualityGateServiceImpl
    ) -> None:
        """set 非法 gate_definitions → ArgumentError。"""
        actor = _actor(roles=["ADMIN"])
        with pytest.raises(ArgumentError):
            await gate_service.set_quality_gate(
                "run-004", [],
                actor_id=actor["user_id"], actor=actor,
            )

    @pytest.mark.asyncio
    async def test_set_quality_gate_permission_denied(
        self, gate_service: QualityGateServiceImpl
    ) -> None:
        """OBSERVER 不能 set（需 write reviews）。"""
        observer = _actor(user_id="obs", roles=["OBSERVER"])
        with pytest.raises(PermissionDeniedError):
            await gate_service.set_quality_gate(
                "run-005", [_gate()],
                actor_id=observer["user_id"], actor=observer,
            )

    @pytest.mark.asyncio
    async def test_get_quality_gate_permission_denied(
        self, gate_service: QualityGateServiceImpl
    ) -> None:
        """无角色用户不能 get。"""
        nobody = _actor(user_id="nobody", roles=[])
        with pytest.raises(PermissionDeniedError):
            await gate_service.get_quality_gate(
                "any",
                actor_id=nobody["user_id"], actor=nobody,
            )


# --------------------------------------------------------------------------- #
# 验收 7：quality_gates 表 DDL 与 artifact_reviews 增强
# --------------------------------------------------------------------------- #


class TestTableSchemas:
    """``quality_gates`` 表与增强 ``artifact_reviews`` 表 DDL 测试。"""

    @pytest.mark.asyncio
    async def test_quality_gates_table_columns(self, db: Database) -> None:
        """quality_gates 表含 id/run_id/node_id/gate_definitions/created_by/
        created_at/version_no 字段。"""
        async with db.write_connection() as conn:
            await conn.execute(
                "INSERT INTO quality_gates "
                "(id, run_id, node_id, gate_definitions, created_by, "
                "created_at, version_no) VALUES (?, ?, ?, ?, ?, ?, 1)",
                (
                    "gate-1",
                    "run-1",
                    None,
                    '[{"name":"g","validator":"v","required_status":"PASS","blocking":true}]',
                    "user-1",
                    "2026-07-17T00:00:00+00:00",
                ),
            )
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT id, run_id, node_id, gate_definitions, created_by, "
                "created_at, version_no FROM quality_gates WHERE id = ?",
                ("gate-1",),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        assert row[0] == "gate-1"
        assert row[1] == "run-1"
        assert row[2] is None
        defs = json.loads(row[3])
        assert defs[0]["name"] == "g"
        assert row[4] == "user-1"
        assert row[5] == "2026-07-17T00:00:00+00:00"
        assert row[6] == 1

    @pytest.mark.asyncio
    async def test_quality_gates_invalid_json_rejected(
        self, db: Database
    ) -> None:
        """CHECK(json_valid(gate_definitions)) 拒绝非 JSON 文本。"""
        async with db.write_connection() as conn:
            with pytest.raises(Exception):  # noqa: BLE001
                await conn.execute(
                    "INSERT INTO quality_gates "
                    "(id, run_id, node_id, gate_definitions, created_by, "
                    "created_at, version_no) VALUES (?, ?, ?, ?, ?, ?, 1)",
                    (
                        "gate-bad",
                        "run-1",
                        None,
                        "not-a-json",
                        "u",
                        "2026-07-17",
                    ),
                )

    @pytest.mark.asyncio
    async def test_artifact_reviews_has_review_status_default(
        self, db: Database
    ) -> None:
        """artifact_reviews.review_status 默认 PENDING。"""
        async with db.write_connection() as conn:
            await conn.execute(
                "INSERT INTO artifact_reviews "
                "(id, artifact_id, status, validator_results, reviewer, "
                "reviewed_at, version_no) VALUES (?, ?, ?, ?, ?, ?, 1)",
                (
                    "rev-1",
                    "art-1",
                    "PASS",
                    "[]",
                    "u",
                    "2026-07-17",
                ),
            )
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT review_status, reviewer_comment, decided_by, decided_at "
                "FROM artifact_reviews WHERE id = ?",
                ("rev-1",),
            ) as cur:
                row = await cur.fetchone()
        assert row is not None
        assert row[0] == "PENDING"  # 默认值
        assert row[1] is None  # reviewer_comment
        assert row[2] is None  # decided_by
        assert row[3] is None  # decided_at


# --------------------------------------------------------------------------- #
# 验收 8：quality_gate 校验辅助
# --------------------------------------------------------------------------- #


class TestQualityGateValidators:
    """``maf_artifact_schemas.quality_gate`` 校验函数测试。"""

    def test_known_review_statuses(self) -> None:
        """KNOWN_REVIEW_STATUSES 含 4 个状态。"""
        assert KNOWN_REVIEW_STATUSES == frozenset(
            {"PENDING", "APPROVED", "REJECTED", "CHANGES_REQUESTED"}
        )

    def test_known_validator_statuses(self) -> None:
        """KNOWN_VALIDATOR_STATUSES 含 3 个状态。"""
        assert KNOWN_VALIDATOR_STATUSES == frozenset({"PASS", "FAIL", "ERROR"})

    def test_validate_gate_name_valid(self) -> None:
        """合法 gate name 通过。"""
        assert validate_gate_name("schema_validation") == "schema_validation"
        assert validate_gate_name("a") == "a"
        assert validate_gate_name("gate-1") == "gate-1"
        assert validate_gate_name("g_v1") == "g_v1"

    def test_validate_gate_name_invalid(self) -> None:
        """非法 gate name 抛 ValueError。"""
        with pytest.raises(ValueError):
            validate_gate_name("")
        with pytest.raises(ValueError):
            validate_gate_name("UpperCase")
        with pytest.raises(ValueError):
            validate_gate_name("1starts_with_digit")
        with pytest.raises(ValueError):
            validate_gate_name("has space")
        with pytest.raises(ValueError):
            validate_gate_name("x" * 65)

    def test_validate_validator_name_valid(self) -> None:
        """合法 validator name 通过（含冒号分隔）。"""
        assert validate_validator_name("hash_integrity") == "hash_integrity"
        assert (
            validate_validator_name("json_schema:task_payload:v1")
            == "json_schema:task_payload:v1"
        )

    def test_validate_validator_name_invalid(self) -> None:
        """非法 validator name 抛 ValueError。"""
        with pytest.raises(ValueError):
            validate_validator_name("")
        with pytest.raises(ValueError):
            validate_validator_name("1starts_with_digit")
        with pytest.raises(ValueError):
            validate_validator_name("has space")

    def test_validate_required_status_valid(self) -> None:
        """合法 required_status 通过。"""
        assert validate_required_status("PASS") == "PASS"
        assert validate_required_status("FAIL") == "FAIL"
        assert validate_required_status("ERROR") == "ERROR"

    def test_validate_required_status_invalid(self) -> None:
        """非法 required_status 抛 ValueError。"""
        with pytest.raises(ValueError):
            validate_required_status("PENDING")  # 不是 Validator 状态
        with pytest.raises(ValueError):
            validate_required_status("pass")  # 大小写敏感
        with pytest.raises(ValueError):
            validate_required_status("")

    def test_validate_gate_definition_valid(self) -> None:
        """合法 GateDefinition 通过。"""
        d = {
            "name": "schema",
            "validator": "json_schema:v1",
            "required_status": "PASS",
            "blocking": True,
        }
        assert validate_gate_definition(d) == d

    def test_validate_gate_definition_invalid(self) -> None:
        """非法 GateDefinition 抛 ValueError。"""
        with pytest.raises(ValueError):
            validate_gate_definition({})  # 缺所有字段
        with pytest.raises(ValueError):
            validate_gate_definition({
                "name": "schema",
                "validator": "v",
                "required_status": "PASS",
                "blocking": "true",  # 应是 bool
            })
        with pytest.raises(ValueError):
            validate_gate_definition({
                "name": "schema",
                "validator": "v",
                "required_status": "INVALID",
                "blocking": True,
            })

    def test_validate_gate_definitions_list(self) -> None:
        """合法列表通过。"""
        defs = [
            {"name": "a", "validator": "v1", "required_status": "PASS",
             "blocking": True},
            {"name": "b", "validator": "v2", "required_status": "FAIL",
             "blocking": False},
        ]
        assert validate_gate_definitions(defs) == defs

    def test_validate_gate_definitions_empty_raises(self) -> None:
        """空列表抛 ValueError。"""
        with pytest.raises(ValueError):
            validate_gate_definitions([])

    def test_validate_gate_definitions_duplicate_name_raises(self) -> None:
        """重复 name 抛 ValueError。"""
        with pytest.raises(ValueError):
            validate_gate_definitions([
                {"name": "dup", "validator": "v1", "required_status": "PASS",
                 "blocking": True},
                {"name": "dup", "validator": "v2", "required_status": "PASS",
                 "blocking": False},
            ])


# --------------------------------------------------------------------------- #
# 验收 9：ensure_reviews_schema 建表辅助
# --------------------------------------------------------------------------- #


class TestEnsureReviewsSchema:
    """``ensure_reviews_schema`` 模块级建表函数测试。"""

    @pytest.mark.asyncio
    async def test_ensure_reviews_schema_idempotent(
        self, tmp_path: Path
    ) -> None:
        """ensure_reviews_schema 幂等建表（多次调用不报错）。"""
        settings = _make_settings(tmp_path)
        database = Database(settings)
        await database.initialize()
        try:
            await ensure_reviews_schema(database)
            # 第二次调用幂等
            await ensure_reviews_schema(database)

            # 表已建好
            async with database.read_connection() as conn:
                async with conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name IN (?, ?)",
                    ("artifact_reviews", "quality_gates"),
                ) as cur:
                    rows = await cur.fetchall()
            table_names = {r[0] for r in rows}
            assert "artifact_reviews" in table_names
            assert "quality_gates" in table_names
        finally:
            await database.close()


# --------------------------------------------------------------------------- #
# 验收 10：端到端集成 —— submit → evaluate → approve
# --------------------------------------------------------------------------- #


class TestEndToEndIntegration:
    """端到端：上传 → 提交评审 → 评估门禁 → 人工批准。"""

    @pytest.mark.asyncio
    async def test_full_flow_pass(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        gate_service: QualityGateServiceImpl,
    ) -> None:
        """完整流程：合规 artifact → 评审 PASS → 门禁 APPROVED → 人工 APPROVED。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("e2e-full-pass")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        # 1. 提交评审（Validator 全 PASS）
        results = [
            ValidatorResult(
                status="PASS",
                issues=[],
                validator_name="hash_integrity",
                validated_at="2026-07-17T00:00:00+00:00",
            ),
        ]
        review_id = await _submit_review(
            review_service, artifact_id,
            results=results, actor=actor,
            comment="auto-validation passed",
        )

        # 2. 评估门禁：blocking PASS → APPROVED
        gates = [_gate(name="hash", validator="hash_integrity",
                       required_status="PASS", blocking=True)]
        gate_result = await gate_service.evaluate(
            artifact_id, gate_definitions=gates,
            actor_id=actor["user_id"], actor=actor,
        )
        assert gate_result["passed"] is True
        assert gate_result["overall_status"] == "APPROVED"

        # 3. 人工批准评审
        approved = await review_service.approve_review(
            review_id,
            actor_id=actor["user_id"], actor=actor,
            comment="auto + manual approved",
        )
        assert approved["review_status"] == "APPROVED"

    @pytest.mark.asyncio
    async def test_full_flow_blocking_failure(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        gate_service: QualityGateServiceImpl,
    ) -> None:
        """完整流程：违规 artifact → 评审 FAIL → 门禁 REJECTED → 人工 REJECTED。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("e2e-blocking-fail")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        # 评审：size FAIL
        results = [
            ValidatorResult(
                status="PASS",
                issues=[],
                validator_name="hash_integrity",
                validated_at="2026-07-17T00:00:00+00:00",
            ),
            ValidatorResult(
                status="FAIL",
                issues=[
                    ValidatorIssue(
                        severity="ERROR",
                        code="size.exceeded",
                        message="too big",
                        path=None,
                    )
                ],
                validator_name="size_limit",
                validated_at="2026-07-17T00:00:00+00:00",
            ),
        ]
        review_id = await _submit_review(
            review_service, artifact_id,
            results=results, actor=actor,
        )

        # 门禁：blocking size 期望 PASS，实际 FAIL → 整体 REJECTED
        gates = [
            _gate(name="hash", validator="hash_integrity",
                  required_status="PASS", blocking=True),
            _gate(name="size", validator="size_limit",
                  required_status="PASS", blocking=True),
        ]
        gate_result = await gate_service.evaluate(
            artifact_id, gate_definitions=gates,
            actor_id=actor["user_id"], actor=actor,
        )
        assert gate_result["passed"] is False
        assert gate_result["overall_status"] == "REJECTED"

        # 人工拒绝
        rejected = await review_service.reject_review(
            review_id,
            actor_id=actor["user_id"], actor=actor,
            comment="gate failed, rejected",
        )
        assert rejected["review_status"] == "REJECTED"

    @pytest.mark.asyncio
    async def test_full_flow_changes_requested_then_approve(
        self,
        artifact_service: ArtifactServiceImpl,
        review_service: ArtifactReviewServiceImpl,
        gate_service: QualityGateServiceImpl,
    ) -> None:
        """完整流程：非阻断失败 → 门禁 CHANGES_REQUESTED → 人工 CHANGES_REQUESTED
        → 重新提交 → APPROVED。"""
        actor = _actor(roles=["ADMIN"])
        content = _content("e2e-changes")
        artifact_id = await _upload(
            artifact_service, content=content, actor=actor
        )

        # 评审：lint 非阻断 FAIL
        results = [
            ValidatorResult(
                status="PASS",
                issues=[],
                validator_name="hash_integrity",
                validated_at="2026-07-17T00:00:00+00:00",
            ),
            ValidatorResult(
                status="FAIL",
                issues=[
                    ValidatorIssue(
                        severity="WARNING",
                        code="lint.warning",
                        message="style",
                        path=None,
                    )
                ],
                validator_name="lint",
                validated_at="2026-07-17T00:00:00+00:00",
            ),
        ]
        review_id = await _submit_review(
            review_service, artifact_id,
            results=results, actor=actor,
        )

        # 门禁：blocking PASS，非阻断 lint FAIL → CHANGES_REQUESTED
        gates = [
            _gate(name="hash", validator="hash_integrity",
                  required_status="PASS", blocking=True),
            _gate(name="lint", validator="lint",
                  required_status="PASS", blocking=False),
        ]
        gate_result = await gate_service.evaluate(
            artifact_id, gate_definitions=gates,
            actor_id=actor["user_id"], actor=actor,
        )
        assert gate_result["passed"] is True  # blocking 全通过
        assert gate_result["overall_status"] == "CHANGES_REQUESTED"

        # 人工 request_changes
        changes = await review_service.request_changes(
            review_id,
            actor_id=actor["user_id"], actor=actor,
            comment="please fix lint",
        )
        assert changes["review_status"] == "CHANGES_REQUESTED"

        # 返工后人工 approve
        approved = await review_service.approve_review(
            review_id,
            actor_id=actor["user_id"], actor=actor,
            comment="fixed, approved",
        )
        assert approved["review_status"] == "APPROVED"
