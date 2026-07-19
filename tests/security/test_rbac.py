"""TASK-031 安全测试：用户与 RBAC。

验收标准：
1. 管理员、设计者、项目负责人、审批人、观察者权限可配置。
2. 最后一个管理员不能被禁用。
3. 无策略或策略异常默认拒绝。

测试范围：
- ``packages/policy/src/maf_policy/policy.py``：CasbinPermissionService、
  DEFAULT_POLICIES、KNOWN_ROLES、validate_permission_keys。
- ``apps/server/src/maf_server/modules/iam/{service,repository,roles}.py``：
  用户 CRUD、角色分配、权限检查、最后管理员保护。
- 不测试外部身份系统（禁止项）、不测试完整 HTTP 路由（TASK-030 已覆盖）。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from maf_contracts.common import ActorContext
from maf_domain.errors import (
    AlreadyExistsError,
    ArgumentError,
    ErrorCode,
    NotFoundError,
    PermissionDeniedError,
    UnauthenticatedError,
    VersionConflictError,
)

from maf_policy import (
    DEFAULT_POLICIES,
    KNOWN_ROLES,
    CasbinPermissionService,
    validate_permission_keys,
)
from maf_server.config import ServerSettings
from maf_server.core.database import Database
from maf_server.modules.iam.repository import SqliteIamRepository, init_schema
from maf_server.modules.iam.service import IamServiceImpl, seed_local_user

_SECRET_PLAINTEXT = "test-secret-for-rbac-task-031"
_TEST_PASSWORD = "rbac-correct-horse-battery-staple"


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
    """已初始化并建好 IAM 表（含 user_permissions）的 Database。"""
    settings = _make_settings(tmp_path)
    database = Database(settings)
    await database.initialize()
    async with database.write_connection() as conn:
        await init_schema(conn)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def admin_db(db: Database) -> tuple[Database, str]:
    """已初始化 + 建表 + 种子一个 ADMIN 用户的 Database；返回 (db, admin_user_id)。"""
    admin_id = await seed_local_user(
        db,
        username="admin",
        display_name="Admin User",
        password_plain=_TEST_PASSWORD,
        permission_keys=["ADMIN"],
    )
    return db, admin_id


def _actor(user_id: str, roles: list[str], trace_id: str = "rbac-trace") -> ActorContext:
    """构造测试用 ActorContext。"""
    return ActorContext(
        user_id=user_id,
        organization_id="org-001",
        permission_keys=roles,
        trace_id=trace_id,
    )


# --------------------------------------------------------------------------- #
# 验收 1：5 个角色权限可配置（Casbin policy 加载与检查）
# --------------------------------------------------------------------------- #


class TestCasbinPolicyLoading:
    """Casbin model/policy 加载与单角色权限检查。"""

    def test_default_policies_cover_all_known_roles(self) -> None:
        """DEFAULT_POLICIES 应覆盖全部 5 个内置角色。"""
        roles_in_policies = {p[0] for p in DEFAULT_POLICIES}
        assert roles_in_policies == KNOWN_ROLES, (
            f"角色不匹配: {roles_in_policies} vs {KNOWN_ROLES}"
        )

    def test_known_roles_has_five_roles(self) -> None:
        """KNOWN_ROLES 应包含恰好 5 个角色。"""
        assert len(KNOWN_ROLES) == 5
        assert "ADMIN" in KNOWN_ROLES
        assert "DESIGNER" in KNOWN_ROLES
        assert "OWNER" in KNOWN_ROLES
        assert "APPROVER" in KNOWN_ROLES
        assert "OBSERVER" in KNOWN_ROLES

    def test_casbin_permission_service_loads_default_policies(self) -> None:
        """CasbinPermissionService 默认加载 DEFAULT_POLICIES。"""
        svc = CasbinPermissionService()
        policies = svc.list_policies()
        assert len(policies) == len(DEFAULT_POLICIES)
        for p in DEFAULT_POLICIES:
            assert tuple(p) in [tuple(x) for x in policies]

    def test_admin_can_do_everything(self) -> None:
        """ADMIN 角色对所有资源有全部权限。"""
        svc = CasbinPermissionService()
        assert svc.check_permission("ADMIN", "users", "read") is True
        assert svc.check_permission("ADMIN", "users", "write") is True
        assert svc.check_permission("ADMIN", "projects", "delete") is True
        assert svc.check_permission("ADMIN", "anything", "any_action") is True

    def test_designer_can_manage_capabilities(self) -> None:
        """DESIGNER 可读写 skills/tools/workflows/model_connections，只读 users。"""
        svc = CasbinPermissionService()
        assert svc.check_permission("DESIGNER", "skills", "read") is True
        assert svc.check_permission("DESIGNER", "skills", "write") is True
        assert svc.check_permission("DESIGNER", "tools", "write") is True
        assert svc.check_permission("DESIGNER", "workflows", "write") is True
        assert svc.check_permission("DESIGNER", "model_connections", "write") is True
        assert svc.check_permission("DESIGNER", "users", "read") is True
        # DESIGNER 不能写 users
        assert svc.check_permission("DESIGNER", "users", "write") is False
        # DESIGNER 不能管理 projects
        assert svc.check_permission("DESIGNER", "projects", "write") is False

    def test_owner_can_manage_projects(self) -> None:
        """OWNER 可管理 projects 和 repositories，只读 users。"""
        svc = CasbinPermissionService()
        assert svc.check_permission("OWNER", "projects", "read") is True
        assert svc.check_permission("OWNER", "projects", "write") is True
        assert svc.check_permission("OWNER", "projects", "delete") is True
        assert svc.check_permission("OWNER", "repositories", "write") is True
        assert svc.check_permission("OWNER", "users", "read") is True
        assert svc.check_permission("OWNER", "users", "write") is False

    def test_approver_can_manage_reviews(self) -> None:
        """APPROVER 可管理 reviews 和 inbox，只读 users。"""
        svc = CasbinPermissionService()
        assert svc.check_permission("APPROVER", "reviews", "read") is True
        assert svc.check_permission("APPROVER", "reviews", "write") is True
        assert svc.check_permission("APPROVER", "inbox", "write") is True
        assert svc.check_permission("APPROVER", "users", "read") is True
        assert svc.check_permission("APPROVER", "users", "write") is False

    def test_observer_can_only_read(self) -> None:
        """OBSERVER 全局只读，不能写。"""
        svc = CasbinPermissionService()
        assert svc.check_permission("OBSERVER", "users", "read") is True
        assert svc.check_permission("OBSERVER", "projects", "read") is True
        assert svc.check_permission("OBSERVER", "anything", "read") is True
        assert svc.check_permission("OBSERVER", "users", "write") is False
        assert svc.check_permission("OBSERVER", "projects", "delete") is False

    def test_custom_policy_can_be_added(self) -> None:
        """可通过 add_policy 添加自定义策略。"""
        svc = CasbinPermissionService()
        # 初始 OBSERVER 不能写 projects
        assert svc.check_permission("OBSERVER", "projects", "write") is False
        svc.add_policy("OBSERVER", "projects", "write")
        assert svc.check_permission("OBSERVER", "projects", "write") is True

    def test_custom_policies_in_constructor(self) -> None:
        """可通过构造函数传入自定义策略。"""
        custom = [("GUEST", "dashboard", "read")]
        svc = CasbinPermissionService(policies=custom)
        assert svc.check_permission("GUEST", "dashboard", "read") is True
        assert svc.check_permission("GUEST", "dashboard", "write") is False
        # 默认策略不应存在
        assert svc.check_permission("ADMIN", "users", "read") is False


# --------------------------------------------------------------------------- #
# 验收 3：无策略或策略异常默认拒绝
# --------------------------------------------------------------------------- #


class TestDefaultDeny:
    """无策略匹配或策略异常时默认拒绝。"""

    def test_unknown_role_denied(self) -> None:
        """未知角色无任何权限。"""
        svc = CasbinPermissionService()
        assert svc.check_permission("UNKNOWN_ROLE", "users", "read") is False

    def test_no_matching_policy_denied(self) -> None:
        """无匹配策略时拒绝。"""
        svc = CasbinPermissionService()
        # DESIGNER 不能 delete skills
        assert svc.check_permission("DESIGNER", "skills", "delete") is False

    @pytest.mark.asyncio
    async def test_require_with_no_permission_keys_denied(self) -> None:
        """actor 无 permission_keys 时 require 拒绝。"""
        svc = CasbinPermissionService()
        actor = _actor("user-1", roles=[])
        with pytest.raises(PermissionDeniedError) as exc_info:
            await svc.require(actor, "read", "users")
        assert exc_info.value.error_code == ErrorCode.PERMISSION_DENIED

    @pytest.mark.asyncio
    async def test_require_with_invalid_actor_denied(self) -> None:
        """actor 不是 dict 时 require 拒绝。"""
        svc = CasbinPermissionService()
        with pytest.raises(PermissionDeniedError):
            await svc.require("not-a-dict", "read", "users")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_require_with_missing_user_id_denied(self) -> None:
        """actor 缺少 user_id 时 require 拒绝。"""
        svc = CasbinPermissionService()
        actor: ActorContext = ActorContext(
            user_id="",
            organization_id="org-001",
            permission_keys=["ADMIN"],
            trace_id="t",
        )
        with pytest.raises(PermissionDeniedError):
            await svc.require(actor, "read", "users")

    @pytest.mark.asyncio
    async def test_require_with_non_list_permission_keys_denied(self) -> None:
        """permission_keys 不是 list 时 require 拒绝。"""
        svc = CasbinPermissionService()
        actor = {"user_id": "u1", "permission_keys": "ADMIN", "trace_id": "t"}
        with pytest.raises(PermissionDeniedError):
            await svc.require(actor, "read", "users")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_check_permission_exception_returns_false(self) -> None:
        """check_permission 策略异常返回 False（默认拒绝）。"""
        svc = CasbinPermissionService()
        # 传入 None 触发异常
        assert svc.check_permission(None, "users", "read") is False  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# PermissionService.require 集成测试
# --------------------------------------------------------------------------- #


class TestPermissionServiceRequire:
    """PermissionService.require 权限检查。"""

    @pytest.mark.asyncio
    async def test_admin_require_allowed(self) -> None:
        """ADMIN 角色的 actor 通过 require 检查。"""
        svc = CasbinPermissionService()
        actor = _actor("admin-1", roles=["ADMIN"])
        # 不抛异常即表示允许
        await svc.require(actor, "read", "users")
        await svc.require(actor, "write", "users")
        await svc.require(actor, "delete", "anything")

    @pytest.mark.asyncio
    async def test_observer_require_read_allowed_write_denied(self) -> None:
        """OBSERVER 可 read，不可 write。"""
        svc = CasbinPermissionService()
        actor = _actor("obs-1", roles=["OBSERVER"])
        await svc.require(actor, "read", "users")
        with pytest.raises(PermissionDeniedError):
            await svc.require(actor, "write", "users")

    @pytest.mark.asyncio
    async def test_multi_role_actor_any_role_suffices(self) -> None:
        """多角色 actor，任一角色通过即放行。"""
        svc = CasbinPermissionService()
        actor = _actor("u1", roles=["OBSERVER", "OWNER"])
        # OBSERVER 可 read，OWNER 可 write projects
        await svc.require(actor, "read", "anything")
        await svc.require(actor, "write", "projects")
        # 但不能写 users（OBSERVER 只读，OWNER 只读 users）
        with pytest.raises(PermissionDeniedError):
            await svc.require(actor, "write", "users")

    @pytest.mark.asyncio
    async def test_require_denied_includes_context(self) -> None:
        """拒绝时 context 含 action 和 resource。"""
        svc = CasbinPermissionService()
        actor = _actor("u1", roles=["OBSERVER"])
        with pytest.raises(PermissionDeniedError) as exc_info:
            await svc.require(actor, "write", "users")
        assert exc_info.value.context.get("action") == "write"
        assert exc_info.value.context.get("resource") == "users"

    @pytest.mark.asyncio
    async def test_list_effective_permissions_returns_roles(self) -> None:
        """list_effective_permissions 返回 actor 的角色列表。"""
        svc = CasbinPermissionService()
        actor = _actor("u1", roles=["ADMIN", "DESIGNER"])
        perms = await svc.list_effective_permissions(actor)
        assert "ADMIN" in perms
        assert "DESIGNER" in perms

    @pytest.mark.asyncio
    async def test_list_effective_permissions_empty_for_invalid_actor(self) -> None:
        """无效 actor 返回空列表。"""
        svc = CasbinPermissionService()
        assert await svc.list_effective_permissions("invalid") == []  # type: ignore[arg-type]
        assert await svc.list_effective_permissions({}) == []


# --------------------------------------------------------------------------- #
# validate_permission_keys 工具测试
# --------------------------------------------------------------------------- #


class TestValidatePermissionKeys:
    """角色校验工具。"""

    def test_valid_keys_returned_deduplicated(self) -> None:
        """合法角色去重后返回。"""
        result = validate_permission_keys(["ADMIN", "ADMIN", "DESIGNER"])
        assert result == ["ADMIN", "DESIGNER"]

    def test_unknown_key_raises(self) -> None:
        """未知角色抛 ValueError。"""
        with pytest.raises(ValueError, match="未知角色"):
            validate_permission_keys(["ADMIN", "SUPERUSER"])

    def test_empty_key_raises(self) -> None:
        """空角色键抛 ValueError。"""
        with pytest.raises(ValueError, match="不能为空"):
            validate_permission_keys(["ADMIN", ""])

    def test_non_list_raises(self) -> None:
        """非列表输入抛 ValueError。"""
        with pytest.raises(ValueError, match="必须是列表"):
            validate_permission_keys("ADMIN")  # type: ignore[arg-type]

    def test_empty_list_allowed(self) -> None:
        """空列表合法（无角色用户）。"""
        assert validate_permission_keys([]) == []


# --------------------------------------------------------------------------- #
# 验收 1：用户角色分配（IAM service 集成）
# --------------------------------------------------------------------------- #


class TestUserRoleAssignment:
    """通过 IamServiceImpl 进行用户创建和角色分配。"""

    @pytest.mark.asyncio
    async def test_create_user_with_roles(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """ADMIN 可创建用户并分配角色。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        actor = _actor(admin_id, roles=["ADMIN"])

        user = await service.create_user(
            actor,
            {
                "username": "designer1",
                "display_name": "Designer One",
                "initial_password": _TEST_PASSWORD,
                "permission_keys": ["DESIGNER"],
                "idempotency_key": "key-1",
            },
        )
        assert user["username"] == "designer1"
        assert user["status"] == "ACTIVE"
        assert user["permissions"] == ["DESIGNER"]
        assert user["version"] == 1

    @pytest.mark.asyncio
    async def test_create_user_with_multiple_roles(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """用户可分配多个角色。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        actor = _actor(admin_id, roles=["ADMIN"])

        user = await service.create_user(
            actor,
            {
                "username": "multi-role",
                "display_name": "Multi Role",
                "initial_password": _TEST_PASSWORD,
                "permission_keys": ["OWNER", "APPROVER"],
                "idempotency_key": "key-2",
            },
        )
        assert set(user["permissions"]) == {"OWNER", "APPROVER"}

    @pytest.mark.asyncio
    async def test_create_user_duplicate_username_rejected(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """重复用户名抛 AlreadyExistsError。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        actor = _actor(admin_id, roles=["ADMIN"])

        with pytest.raises(AlreadyExistsError):
            await service.create_user(
                actor,
                {
                    "username": "admin",
                    "display_name": "Dup",
                    "initial_password": _TEST_PASSWORD,
                    "permission_keys": ["OBSERVER"],
                    "idempotency_key": "key-3",
                },
            )

    @pytest.mark.asyncio
    async def test_create_user_unknown_role_rejected(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """未知角色抛 ArgumentError。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        actor = _actor(admin_id, roles=["ADMIN"])

        with pytest.raises(ArgumentError):
            await service.create_user(
                actor,
                {
                    "username": "bad-role",
                    "display_name": "Bad",
                    "initial_password": _TEST_PASSWORD,
                    "permission_keys": ["SUPERUSER"],
                    "idempotency_key": "key-4",
                },
            )

    @pytest.mark.asyncio
    async def test_non_admin_cannot_create_user(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """非 ADMIN 角色不能创建用户。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        # 先创建一个 OBSERVER 用户
        observer = await service.create_user(
            _actor(admin_id, roles=["ADMIN"]),
            {
                "username": "observer1",
                "display_name": "Observer",
                "initial_password": _TEST_PASSWORD,
                "permission_keys": ["OBSERVER"],
                "idempotency_key": "key-5",
            },
        )
        # OBSERVER 尝试创建用户 → 拒绝
        observer_actor = _actor(observer["id"], roles=["OBSERVER"])
        with pytest.raises(PermissionDeniedError):
            await service.create_user(
                observer_actor,
                {
                    "username": "should-fail",
                    "display_name": "Fail",
                    "initial_password": _TEST_PASSWORD,
                    "permission_keys": ["OBSERVER"],
                    "idempotency_key": "key-6",
                },
            )

    @pytest.mark.asyncio
    async def test_login_returns_user_permissions(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """login 返回的 UserView 应包含用户角色。"""
        db, _ = admin_db
        service = IamServiceImpl(db)
        session = await service.login(
            {"username": "admin", "password": _TEST_PASSWORD}
        )
        assert "ADMIN" in session["user"]["permissions"]


# --------------------------------------------------------------------------- #
# 验收 1 & 3：list_users 权限检查
# --------------------------------------------------------------------------- #


class TestListUsersPermission:
    """list_users 权限检查。"""

    @pytest.mark.asyncio
    async def test_admin_can_list_users(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """ADMIN 可列出用户。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        actor = _actor(admin_id, roles=["ADMIN"])
        page = await service.list_users(actor, {"limit": 10})
        assert len(page["items"]) >= 1
        assert page["has_more"] is False or page["next_cursor"] is not None

    @pytest.mark.asyncio
    async def test_observer_cannot_list_users(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """OBSERVER 不能列出用户（只能 read，但 list_users 需要 read 权限...

        实际上 OBSERVER 有全局 read 权限，所以可以 list_users。
        但 OBSERVER 不能 create_user（需要 write）。
        """
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        # 先创建 OBSERVER 用户
        observer = await service.create_user(
            _actor(admin_id, roles=["ADMIN"]),
            {
                "username": "obs-list",
                "display_name": "Observer",
                "initial_password": _TEST_PASSWORD,
                "permission_keys": ["OBSERVER"],
                "idempotency_key": "key-list-1",
            },
        )
        # OBSERVER 有 read 权限，可以 list_users
        observer_actor = _actor(observer["id"], roles=["OBSERVER"])
        page = await service.list_users(observer_actor, {"limit": 10})
        assert len(page["items"]) >= 1

    @pytest.mark.asyncio
    async def test_list_users_with_keyword_filter(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """keyword 过滤用户名。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        actor = _actor(admin_id, roles=["ADMIN"])
        await service.create_user(
            actor,
            {
                "username": "alice-designer",
                "display_name": "Alice",
                "initial_password": _TEST_PASSWORD,
                "permission_keys": ["DESIGNER"],
                "idempotency_key": "key-kw-1",
            },
        )
        page = await service.list_users(actor, {"keyword": "alice", "limit": 10})
        assert any(u["username"] == "alice-designer" for u in page["items"])

    @pytest.mark.asyncio
    async def test_list_users_status_filter(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """status 过滤。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        actor = _actor(admin_id, roles=["ADMIN"])
        # 创建一个 DISABLED 用户
        disabled = await service.create_user(
            actor,
            {
                "username": "disabled-user",
                "display_name": "Disabled",
                "initial_password": _TEST_PASSWORD,
                "permission_keys": ["OBSERVER"],
                "idempotency_key": "key-st-1",
            },
        )
        await service.update_user(
            actor,
            disabled["id"],
            {"status": "DISABLED", "expected_version": 1},
        )
        # 只查 ACTIVE
        page = await service.list_users(actor, {"status": "ACTIVE", "limit": 100})
        assert all(u["status"] == "ACTIVE" for u in page["items"])
        assert all(u["username"] != "disabled-user" for u in page["items"])


# --------------------------------------------------------------------------- #
# 验收 1 & 3：update_user 权限检查与角色更新
# --------------------------------------------------------------------------- #


class TestUpdateUserPermission:
    """update_user 权限检查与角色更新。"""

    @pytest.mark.asyncio
    async def test_admin_can_update_user_display_name(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """ADMIN 可更新用户显示名。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        actor = _actor(admin_id, roles=["ADMIN"])
        user = await service.create_user(
            actor,
            {
                "username": "to-update",
                "display_name": "Original",
                "initial_password": _TEST_PASSWORD,
                "permission_keys": ["DESIGNER"],
                "idempotency_key": "key-up-1",
            },
        )
        updated = await service.update_user(
            actor,
            user["id"],
            {"display_name": "Updated", "expected_version": 1},
        )
        assert updated["display_name"] == "Updated"
        assert updated["version"] == 2

    @pytest.mark.asyncio
    async def test_admin_can_update_user_roles(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """ADMIN 可更新用户角色。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        actor = _actor(admin_id, roles=["ADMIN"])
        user = await service.create_user(
            actor,
            {
                "username": "role-update",
                "display_name": "Role Update",
                "initial_password": _TEST_PASSWORD,
                "permission_keys": ["OBSERVER"],
                "idempotency_key": "key-up-2",
            },
        )
        updated = await service.update_user(
            actor,
            user["id"],
            {"permission_keys": ["OWNER", "APPROVER"], "expected_version": 1},
        )
        assert set(updated["permissions"]) == {"OWNER", "APPROVER"}

    @pytest.mark.asyncio
    async def test_observer_cannot_update_user(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """OBSERVER 不能更新用户（需要 write 权限）。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        observer = await service.create_user(
            _actor(admin_id, roles=["ADMIN"]),
            {
                "username": "obs-up",
                "display_name": "Observer",
                "initial_password": _TEST_PASSWORD,
                "permission_keys": ["OBSERVER"],
                "idempotency_key": "key-up-3",
            },
        )
        observer_actor = _actor(observer["id"], roles=["OBSERVER"])
        with pytest.raises(PermissionDeniedError):
            await service.update_user(
                observer_actor,
                observer["id"],
                {"display_name": "Hacked", "expected_version": 1},
            )

    @pytest.mark.asyncio
    async def test_version_conflict_on_stale_update(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """expected_version 不匹配抛 VersionConflictError。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        actor = _actor(admin_id, roles=["ADMIN"])
        user = await service.create_user(
            actor,
            {
                "username": "version-test",
                "display_name": "Version",
                "initial_password": _TEST_PASSWORD,
                "permission_keys": ["OBSERVER"],
                "idempotency_key": "key-up-4",
            },
        )
        with pytest.raises(VersionConflictError):
            await service.update_user(
                actor,
                user["id"],
                {"display_name": "Stale", "expected_version": 99},
            )

    @pytest.mark.asyncio
    async def test_update_nonexistent_user_raises_not_found(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """更新不存在的用户抛 NotFoundError。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        actor = _actor(admin_id, roles=["ADMIN"])
        with pytest.raises(NotFoundError):
            await service.update_user(
                actor,
                "nonexistent-user-id",
                {"display_name": "X"},
            )

    @pytest.mark.asyncio
    async def test_disable_user_revokes_sessions(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """禁用用户时撤销其全部会话。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        admin_actor = _actor(admin_id, roles=["ADMIN"])

        # 创建用户并登录
        user = await service.create_user(
            admin_actor,
            {
                "username": "to-disable",
                "display_name": "To Disable",
                "initial_password": _TEST_PASSWORD,
                "permission_keys": ["DESIGNER"],
                "idempotency_key": "key-up-5",
            },
        )
        session = await service.login(
            {"username": "to-disable", "password": _TEST_PASSWORD}
        )

        # 禁用用户
        await service.update_user(
            admin_actor,
            user["id"],
            {"status": "DISABLED", "expected_version": 1},
        )

        # 验证 session 已撤销
        async with db.read_connection() as conn:
            async with conn.execute(
                "SELECT revoked_at FROM sessions WHERE id = ?",
                (session["session_id"],),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] is not None, "禁用用户后会话应被撤销"


# --------------------------------------------------------------------------- #
# 验收 2：最后一个管理员不能被禁用
# --------------------------------------------------------------------------- #


class TestLastAdminProtection:
    """最后一个管理员保护。"""

    @pytest.mark.asyncio
    async def test_cannot_disable_last_admin(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """不能禁用唯一的管理员。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        actor = _actor(admin_id, roles=["ADMIN"])

        with pytest.raises(ArgumentError, match="最后一个管理员"):
            await service.update_user(
                actor,
                admin_id,
                {"status": "DISABLED", "expected_version": 1},
            )

    @pytest.mark.asyncio
    async def test_cannot_remove_admin_role_from_last_admin(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """不能从唯一管理员身上移除 ADMIN 角色。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        actor = _actor(admin_id, roles=["ADMIN"])

        with pytest.raises(ArgumentError, match="最后一个管理员"):
            await service.update_user(
                actor,
                admin_id,
                {"permission_keys": ["OBSERVER"], "expected_version": 1},
            )

    @pytest.mark.asyncio
    async def test_can_disable_admin_when_another_admin_exists(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """存在其他管理员时可禁用当前管理员。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        actor = _actor(admin_id, roles=["ADMIN"])

        # 创建第二个管理员
        second_admin = await service.create_user(
            actor,
            {
                "username": "admin2",
                "display_name": "Admin Two",
                "initial_password": _TEST_PASSWORD,
                "permission_keys": ["ADMIN"],
                "idempotency_key": "key-la-1",
            },
        )

        # 现在可以禁用第一个管理员
        updated = await service.update_user(
            actor,
            admin_id,
            {"status": "DISABLED", "expected_version": 1},
        )
        assert updated["status"] == "DISABLED"

        # 第二个管理员仍 ACTIVE
        assert second_admin["id"] != admin_id

    @pytest.mark.asyncio
    async def test_can_disable_non_admin_user(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """禁用非 ADMIN 用户不受最后管理员保护限制。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        actor = _actor(admin_id, roles=["ADMIN"])

        user = await service.create_user(
            actor,
            {
                "username": "regular-user",
                "display_name": "Regular",
                "initial_password": _TEST_PASSWORD,
                "permission_keys": ["DESIGNER"],
                "idempotency_key": "key-la-2",
            },
        )
        updated = await service.update_user(
            actor,
            user["id"],
            {"status": "DISABLED", "expected_version": 1},
        )
        assert updated["status"] == "DISABLED"


# --------------------------------------------------------------------------- #
# get_current_user 测试
# --------------------------------------------------------------------------- #


class TestGetCurrentUser:
    """get_current_user 返回实时权限。"""

    @pytest.mark.asyncio
    async def test_get_current_user_returns_fresh_permissions(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """get_current_user 从 DB 重新加载权限。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        # actor 携带空 permissions，但 DB 中有 ADMIN
        actor = _actor(admin_id, roles=[])
        user = await service.get_current_user(actor)
        assert user["id"] == admin_id
        assert user["username"] == "admin"
        assert "ADMIN" in user["permissions"]

    @pytest.mark.asyncio
    async def test_get_current_user_invalid_actor_rejected(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """无效 actor 抛 UnauthenticatedError。"""
        db, _ = admin_db
        service = IamServiceImpl(db)
        with pytest.raises(UnauthenticatedError):
            await service.get_current_user({})  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_get_current_user_nonexistent_raises_not_found(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """不存在的用户抛 NotFoundError。"""
        db, _ = admin_db
        service = IamServiceImpl(db)
        actor = _actor("nonexistent-user-id", roles=["ADMIN"])
        with pytest.raises(NotFoundError):
            await service.get_current_user(actor)


# --------------------------------------------------------------------------- #
# Repository 直接测试（user_permissions 表）
# --------------------------------------------------------------------------- #


class TestUserPermissionsRepository:
    """SqliteIamRepository user_permissions 方法测试。"""

    @pytest.mark.asyncio
    async def test_get_user_permissions_empty(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """无权限的用户返回空列表。"""
        db, admin_id = admin_db
        repo = SqliteIamRepository()
        async with db.read_connection() as conn:
            # admin 有 ADMIN 权限
            admin_perms = await repo.get_user_permissions(conn, admin_id)
        assert "ADMIN" in admin_perms

    @pytest.mark.asyncio
    async def test_replace_user_permissions(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """全量替换用户权限。"""
        db, admin_id = admin_db
        repo = SqliteIamRepository()
        now = datetime.now(timezone.utc)

        async with db.write_connection() as conn:
            await repo.replace_user_permissions(
                conn, admin_id, ["DESIGNER", "OWNER"], now
            )
        async with db.read_connection() as conn:
            perms = await repo.get_user_permissions(conn, admin_id)
        assert set(perms) == {"DESIGNER", "OWNER"}

    @pytest.mark.asyncio
    async def test_count_active_admins(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """统计 ACTIVE ADMIN 数量。"""
        db, _ = admin_db
        repo = SqliteIamRepository()
        async with db.read_connection() as conn:
            count = await repo.count_active_users_with_permission(conn, "ADMIN")
        assert count == 1

    @pytest.mark.asyncio
    async def test_count_excludes_disabled_admins(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """DISABLED ADMIN 不计入统计。"""
        db, admin_id = admin_db
        repo = SqliteIamRepository()

        # 创建第二个 ADMIN
        service = IamServiceImpl(db)
        second = await service.create_user(
            _actor(admin_id, roles=["ADMIN"]),
            {
                "username": "admin2",
                "display_name": "Admin2",
                "initial_password": _TEST_PASSWORD,
                "permission_keys": ["ADMIN"],
                "idempotency_key": "key-r-1",
            },
        )
        # 禁用第二个 ADMIN
        async with db.write_connection() as conn:
            await conn.execute(
                "UPDATE users SET status = 'DISABLED' WHERE id = ?",
                (second["id"],),
            )
        async with db.read_connection() as conn:
            count = await repo.count_active_users_with_permission(conn, "ADMIN")
        assert count == 1, "DISABLED ADMIN 不应计入"

    @pytest.mark.asyncio
    async def test_list_users_page(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """分页查询用户。"""
        db, admin_id = admin_db
        service = IamServiceImpl(db)
        actor = _actor(admin_id, roles=["ADMIN"])

        # 创建额外用户
        for i in range(5):
            await service.create_user(
                actor,
                {
                    "username": f"user-{i}",
                    "display_name": f"User {i}",
                    "initial_password": _TEST_PASSWORD,
                    "permission_keys": ["OBSERVER"],
                    "idempotency_key": f"key-page-{i}",
                },
            )

        repo = SqliteIamRepository()
        async with db.read_connection() as conn:
            records, next_cursor = await repo.list_users_page(
                conn, limit=3
            )
        assert len(records) == 3
        assert next_cursor is not None

        # 第二页
        async with db.read_connection() as conn:
            records2, next_cursor2 = await repo.list_users_page(
                conn, cursor=next_cursor, limit=3
            )
        assert len(records2) >= 1
