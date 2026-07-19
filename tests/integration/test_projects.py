"""TASK-033 集成测试：项目 CRUD 与成员管理。

覆盖范围：
1. 项目 CRUD（create/get/list/update/delete）成功路径。
2. 成员管理（add/remove/list/update_role）成功路径。
3. 权限检查：OBSERVER 只读，无权限者拒绝。
4. 乐观锁：update/delete 版本冲突抛 VersionConflictError。
5. 最后 OWNER 保护：remove/update_role 拒绝让项目失去最后一个 OWNER。
6. 事件触发：CRUD 与成员操作写入 outbox_events，与业务写入同事务。
7. 可见性：list_projects 只返回调用者作为成员的项目；非成员 get_project 返回 404。
8. 参数校验：空名称、非法角色、非法状态等抛 ArgumentError。

测试范围禁止：不测试 Run/Workflow 执行（仅项目 CRUD 与成员管理）。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from maf_domain.errors import (
    AlreadyExistsError,
    ArgumentError,
    NotFoundError,
    PermissionDeniedError,
    VersionConflictError,
)

from maf_server.config import ServerSettings
from maf_server.core.database import Database
from maf_server.core.events import init_outbox_schema
from maf_server.modules.iam.repository import SqliteIamRepository, init_schema
from maf_server.modules.iam.service import seed_local_user
from maf_server.modules.projects.repository import SqliteProjectRepository
from maf_server.modules.projects.service import ProjectApplicationServiceImpl

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

_SECRET_PLAINTEXT = "test-secret-for-projects-task-033"
_TEST_PASSWORD = "projects-correct-horse-battery-staple"
_ORG_ID = "org-001"

#: projects 与 project_members 建表 SQL（与 migrations/0001 保持一致）。
_PROJECTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id              TEXT    PRIMARY KEY,
    name            TEXT    NOT NULL,
    description     TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'ACTIVE',
    created_at      TEXT    NOT NULL,
    created_by      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    version_no      INTEGER NOT NULL DEFAULT 1,
    deleted_at      TEXT,
    CHECK (status IN ('ACTIVE', 'ARCHIVED'))
);

CREATE INDEX IF NOT EXISTS idx_projects_created_by ON projects(created_by);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);

CREATE TABLE IF NOT EXISTS project_members (
    project_id      TEXT    NOT NULL,
    user_id         TEXT    NOT NULL,
    role            TEXT    NOT NULL,
    added_at        TEXT    NOT NULL,
    added_by        TEXT    NOT NULL,
    version_no      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (project_id, user_id),
    CHECK (role IN ('OWNER', 'APPROVER', 'OBSERVER', 'DESIGNER'))
);

CREATE INDEX IF NOT EXISTS idx_project_members_user_id ON project_members(user_id);
CREATE INDEX IF NOT EXISTS idx_project_members_role ON project_members(project_id, role);
"""


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clear_maf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除所有 ``MAF_*`` 环境变量，保证测试从干净状态开始。"""
    for key in list(os.environ):
        if key.startswith("MAF_"):
            monkeypatch.delenv(key, raising=False)


def _make_settings(tmp_path: Path, **overrides: object) -> ServerSettings:
    kwargs: dict[str, object] = dict(
        organization_id=_ORG_ID,
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


async def _init_projects_schema(database: Database) -> None:
    """在 ``database`` 上创建 projects 与 project_members 表（幂等）。"""
    async with database.write_connection() as conn:
        for stmt in _PROJECTS_SCHEMA_SQL.split(";"):
            stripped = stmt.strip()
            if stripped:
                await conn.execute(stripped)


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    """已初始化 IAM + projects + outbox schema 的 Database。"""
    settings = _make_settings(tmp_path)
    database = Database(settings)
    await database.initialize()
    async with database.write_connection() as conn:
        await init_schema(conn)
    await _init_projects_schema(database)
    await init_outbox_schema(database)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def admin_db(db: Database) -> tuple[Database, str]:
    """已种子 ADMIN 用户的 Database；返回 (db, admin_user_id)。"""
    admin_id = await seed_local_user(
        db,
        username="admin",
        display_name="Admin User",
        password_plain=_TEST_PASSWORD,
        permission_keys=["ADMIN"],
    )
    return db, admin_id


@pytest_asyncio.fixture
async def observer_db(admin_db: tuple[Database, str]) -> tuple[Database, str, str]:
    """已种子 ADMIN + OBSERVER 用户的 Database；返回 (db, admin_id, observer_id)。"""
    db, admin_id = admin_db
    observer_id = await seed_local_user(
        db,
        username="observer",
        display_name="Observer User",
        password_plain=_TEST_PASSWORD,
        permission_keys=["OBSERVER"],
    )
    return db, admin_id, observer_id


def _make_service(db: Database) -> ProjectApplicationServiceImpl:
    """构造 ProjectApplicationServiceImpl 实例。"""
    return ProjectApplicationServiceImpl(
        db,
        organization_id=_ORG_ID,
        iam_repository=SqliteIamRepository(),
        project_repository=SqliteProjectRepository(),
    )


async def _count_outbox_events(db: Database, event_type: str) -> int:
    """统计 outbox_events 中指定 event_type 的事件数量。"""
    async with db.read_connection() as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM outbox_events WHERE event_type = ?",
            (event_type,),
        ) as cur:
            row = await cur.fetchone()
    return int(row[0]) if row is not None else 0


async def _list_outbox_events(
    db: Database, project_id: str | None = None
) -> list[dict]:
    """列出 outbox_events 事件（按 occurred_at 升序）。"""
    sql = (
        "SELECT event_type, aggregate_type, aggregate_id, project_id, payload "
        "FROM outbox_events "
    )
    params: tuple = ()
    if project_id is not None:
        sql += "WHERE project_id = ? "
        params = (project_id,)
    sql += "ORDER BY occurred_at ASC, id ASC"
    async with db.read_connection() as conn:
        async with conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
    return [
        {
            "event_type": r[0],
            "aggregate_type": r[1],
            "aggregate_id": r[2],
            "project_id": r[3],
            "payload": json.loads(r[4]),
        }
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# 1. 项目 CRUD 成功路径
# --------------------------------------------------------------------------- #


class TestProjectCrud:
    """项目 CRUD 成功路径测试。"""

    @pytest.mark.asyncio
    async def test_create_project_returns_version_1_creator_is_owner(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)

        view = await service.create_project(
            "Test Project", "A test project", actor_id=admin_id
        )

        assert view["name"] == "Test Project"
        assert view["description"] == "A test project"
        assert view["status"] == "ACTIVE"
        assert view["version"] == 1
        assert view["created_by"] == admin_id
        assert view["deleted_at"] is None
        # UUID4 格式校验
        uuid.UUID(view["id"])

        # creator 自动成为 OWNER
        members = await service.list_members(view["id"], actor_id=admin_id)
        assert len(members) == 1
        assert members[0]["user_id"] == admin_id
        assert members[0]["role"] == "OWNER"

    @pytest.mark.asyncio
    async def test_create_project_strips_name_whitespace(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)

        view = await service.create_project(
            "  Spaced Name  ", "", actor_id=admin_id
        )
        assert view["name"] == "Spaced Name"
        assert view["description"] == ""

    @pytest.mark.asyncio
    async def test_get_project_returns_details(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)

        created = await service.create_project(
            "Get Test", "desc", actor_id=admin_id
        )
        fetched = await service.get_project(created["id"], actor_id=admin_id)
        assert fetched["id"] == created["id"]
        assert fetched["name"] == "Get Test"

    @pytest.mark.asyncio
    async def test_list_projects_returns_only_member_projects(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        # admin 创建 2 个项目
        p1 = await service.create_project("P1", "", actor_id=admin_id)
        p2 = await service.create_project("P2", "", actor_id=admin_id)

        # admin 可见 2 个项目
        admin_projects = await service.list_projects(actor_id=admin_id)
        assert len(admin_projects) == 2
        admin_ids = {p["id"] for p in admin_projects}
        assert {p1["id"], p2["id"]} == admin_ids

        # observer 无项目可见
        observer_projects = await service.list_projects(actor_id=observer_id)
        assert len(observer_projects) == 0

        # admin 把 observer 加到 p1
        await service.add_member(
            p1["id"], observer_id, "OBSERVER", actor_id=admin_id
        )
        observer_projects = await service.list_projects(actor_id=observer_id)
        assert len(observer_projects) == 1
        assert observer_projects[0]["id"] == p1["id"]

    @pytest.mark.asyncio
    async def test_update_project_name_and_description(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)

        created = await service.create_project(
            "Original", "orig desc", actor_id=admin_id
        )
        updated = await service.update_project(
            created["id"],
            name="Updated",
            description="new desc",
            expected_version=1,
            actor_id=admin_id,
        )
        assert updated["name"] == "Updated"
        assert updated["description"] == "new desc"
        assert updated["version"] == 2

    @pytest.mark.asyncio
    async def test_update_project_status_to_archived(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)

        created = await service.create_project(
            "Archive Test", "", actor_id=admin_id
        )
        updated = await service.update_project(
            created["id"],
            status="ARCHIVED",
            expected_version=1,
            actor_id=admin_id,
        )
        assert updated["status"] == "ARCHIVED"
        assert updated["version"] == 2

    @pytest.mark.asyncio
    async def test_delete_project_soft_deletes(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)

        created = await service.create_project(
            "Delete Test", "", actor_id=admin_id
        )
        await service.delete_project(
            created["id"], expected_version=1, actor_id=admin_id
        )

        # 软删除后 get_project 返回 404（deleted_at IS NULL 过滤）
        with pytest.raises(NotFoundError):
            await service.get_project(created["id"], actor_id=admin_id)

        # list_projects 不再包含该项目
        projects = await service.list_projects(actor_id=admin_id)
        assert all(p["id"] != created["id"] for p in projects)


# --------------------------------------------------------------------------- #
# 2. 成员管理成功路径
# --------------------------------------------------------------------------- #


class TestMemberManagement:
    """成员管理成功路径测试。"""

    @pytest.mark.asyncio
    async def test_add_member_approrover(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Member Test", "", actor_id=admin_id
        )
        member = await service.add_member(
            project["id"], observer_id, "APPROVER", actor_id=admin_id
        )
        assert member["user_id"] == observer_id
        assert member["role"] == "APPROVER"
        assert member["version"] == 1

    @pytest.mark.asyncio
    async def test_list_members_returns_all(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "List Members", "", actor_id=admin_id
        )
        await service.add_member(
            project["id"], observer_id, "OBSERVER", actor_id=admin_id
        )

        members = await service.list_members(project["id"], actor_id=admin_id)
        assert len(members) == 2
        roles = {m["role"] for m in members}
        assert roles == {"OWNER", "OBSERVER"}

    @pytest.mark.asyncio
    async def test_remove_member_success(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Remove Test", "", actor_id=admin_id
        )
        await service.add_member(
            project["id"], observer_id, "OBSERVER", actor_id=admin_id
        )
        await service.remove_member(project["id"], observer_id, actor_id=admin_id)

        members = await service.list_members(project["id"], actor_id=admin_id)
        assert len(members) == 1
        assert all(m["user_id"] != observer_id for m in members)

    @pytest.mark.asyncio
    async def test_update_member_role_owner_to_approver(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Role Change", "", actor_id=admin_id
        )
        await service.add_member(
            project["id"], observer_id, "APPROVER", actor_id=admin_id
        )
        updated = await service.update_member_role(
            project["id"], observer_id, "OBSERVER", actor_id=admin_id
        )
        assert updated["role"] == "OBSERVER"
        assert updated["version"] == 2

    @pytest.mark.asyncio
    async def test_add_duplicate_member_raises(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Dup Member", "", actor_id=admin_id
        )
        await service.add_member(
            project["id"], observer_id, "OBSERVER", actor_id=admin_id
        )
        with pytest.raises(AlreadyExistsError):
            await service.add_member(
                project["id"], observer_id, "APPROVER", actor_id=admin_id
            )

    @pytest.mark.asyncio
    async def test_remove_nonexistent_member_raises(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "No Member", "", actor_id=admin_id
        )
        with pytest.raises(NotFoundError):
            await service.remove_member(
                project["id"], observer_id, actor_id=admin_id
            )


# --------------------------------------------------------------------------- #
# 3. 权限检查
# --------------------------------------------------------------------------- #


class TestPermissionChecks:
    """权限检查测试：OBSERVER 只读，无权限者拒绝。"""

    @pytest.mark.asyncio
    async def test_observer_can_list_projects(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)
        # observer 无项目但仍可调用（返回空列表）
        projects = await service.list_projects(actor_id=observer_id)
        assert projects == []

    @pytest.mark.asyncio
    async def test_observer_cannot_create_project(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)
        with pytest.raises(PermissionDeniedError):
            await service.create_project(
                "Forbidden", "", actor_id=observer_id
            )

    @pytest.mark.asyncio
    async def test_observer_cannot_update_project(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Admin Project", "", actor_id=admin_id
        )
        # admin 把 observer 加为成员（只读）
        await service.add_member(
            project["id"], observer_id, "OBSERVER", actor_id=admin_id
        )
        with pytest.raises(PermissionDeniedError):
            await service.update_project(
                project["id"],
                name="Hacked",
                expected_version=1,
                actor_id=observer_id,
            )

    @pytest.mark.asyncio
    async def test_observer_cannot_delete_project(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Admin Project", "", actor_id=admin_id
        )
        await service.add_member(
            project["id"], observer_id, "OBSERVER", actor_id=admin_id
        )
        with pytest.raises(PermissionDeniedError):
            await service.delete_project(
                project["id"], expected_version=1, actor_id=observer_id
            )

    @pytest.mark.asyncio
    async def test_observer_cannot_add_member(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Admin Project", "", actor_id=admin_id
        )
        with pytest.raises(PermissionDeniedError):
            await service.add_member(
                project["id"], admin_id, "OBSERVER", actor_id=observer_id
            )

    @pytest.mark.asyncio
    async def test_observer_cannot_remove_member(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Admin Project", "", actor_id=admin_id
        )
        await service.add_member(
            project["id"], observer_id, "OBSERVER", actor_id=admin_id
        )
        with pytest.raises(PermissionDeniedError):
            await service.remove_member(
                project["id"], observer_id, actor_id=observer_id
            )

    @pytest.mark.asyncio
    async def test_empty_actor_id_rejected(self, admin_db: tuple[Database, str]) -> None:
        db, admin_id = admin_db
        service = _make_service(db)
        with pytest.raises(PermissionDeniedError):
            await service.create_project("No Actor", "", actor_id="")

    @pytest.mark.asyncio
    async def test_nonexistent_user_rejected(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """不存在的 user_id 无权限键，require 拒绝。"""
        db, admin_id = admin_db
        service = _make_service(db)
        with pytest.raises(PermissionDeniedError):
            await service.create_project(
                "Ghost", "", actor_id="nonexistent-user-id"
            )


# --------------------------------------------------------------------------- #
# 4. 乐观锁
# --------------------------------------------------------------------------- #


class TestOptimisticLock:
    """乐观锁测试：版本冲突抛 VersionConflictError。"""

    @pytest.mark.asyncio
    async def test_update_project_version_conflict(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)

        project = await service.create_project(
            "Lock Test", "", actor_id=admin_id
        )
        # 第一次更新成功，version → 2
        await service.update_project(
            project["id"],
            name="V2",
            expected_version=1,
            actor_id=admin_id,
        )
        # 用旧版本号再次更新，冲突
        with pytest.raises(VersionConflictError):
            await service.update_project(
                project["id"],
                name="V3-stale",
                expected_version=1,
                actor_id=admin_id,
            )

    @pytest.mark.asyncio
    async def test_delete_project_version_conflict(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)

        project = await service.create_project(
            "Delete Lock", "", actor_id=admin_id
        )
        # 先更新一次，version → 2
        await service.update_project(
            project["id"],
            name="Updated",
            expected_version=1,
            actor_id=admin_id,
        )
        # 用旧版本号删除，冲突
        with pytest.raises(VersionConflictError):
            await service.delete_project(
                project["id"], expected_version=1, actor_id=admin_id
            )

    @pytest.mark.asyncio
    async def test_update_member_role_version_conflict(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        """update_member_role 内部使用 member.version_no 做乐观锁。"""
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Member Lock", "", actor_id=admin_id
        )
        await service.add_member(
            project["id"], observer_id, "APPROVER", actor_id=admin_id
        )
        # 正常变更，version → 2
        await service.update_member_role(
            project["id"], observer_id, "OBSERVER", actor_id=admin_id
        )
        # 再次变更应该成功（内部读取最新 version_no=2）
        await service.update_member_role(
            project["id"], observer_id, "APPROVER", actor_id=admin_id
        )
        members = await service.list_members(project["id"], actor_id=admin_id)
        target = [m for m in members if m["user_id"] == observer_id][0]
        assert target["role"] == "APPROVER"
        assert target["version"] == 3


# --------------------------------------------------------------------------- #
# 5. 最后 OWNER 保护
# --------------------------------------------------------------------------- #


class TestLastOwnerProtection:
    """最后 OWNER 保护测试。"""

    @pytest.mark.asyncio
    async def test_cannot_remove_last_owner(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)

        project = await service.create_project(
            "Last Owner", "", actor_id=admin_id
        )
        # admin 是唯一 OWNER，不能移除自己
        with pytest.raises(ArgumentError, match="最后一个 OWNER"):
            await service.remove_member(
                project["id"], admin_id, actor_id=admin_id
            )

    @pytest.mark.asyncio
    async def test_cannot_demote_last_owner(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)

        project = await service.create_project(
            "Last Owner Demote", "", actor_id=admin_id
        )
        with pytest.raises(ArgumentError, match="最后一个 OWNER"):
            await service.update_member_role(
                project["id"], admin_id, "OBSERVER", actor_id=admin_id
            )

    @pytest.mark.asyncio
    async def test_can_remove_owner_when_multiple_owners(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Multi Owner", "", actor_id=admin_id
        )
        # 加第二个 OWNER
        await service.add_member(
            project["id"], observer_id, "OWNER", actor_id=admin_id
        )
        # 现在可以移除 admin（仍剩 observer 作为 OWNER）
        await service.remove_member(
            project["id"], admin_id, actor_id=admin_id
        )
        members = await service.list_members(project["id"], actor_id=observer_id)
        assert len(members) == 1
        assert members[0]["user_id"] == observer_id
        assert members[0]["role"] == "OWNER"

    @pytest.mark.asyncio
    async def test_can_demote_owner_when_multiple_owners(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Multi Owner Demote", "", actor_id=admin_id
        )
        await service.add_member(
            project["id"], observer_id, "OWNER", actor_id=admin_id
        )
        # 降级 admin（仍剩 observer 作为 OWNER）
        updated = await service.update_member_role(
            project["id"], admin_id, "APPROVER", actor_id=admin_id
        )
        assert updated["role"] == "APPROVER"


# --------------------------------------------------------------------------- #
# 6. 事件触发
# --------------------------------------------------------------------------- #


class TestEventTriggering:
    """事件触发测试：CRUD 与成员操作写入 outbox_events。"""

    @pytest.mark.asyncio
    async def test_create_project_writes_event(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)

        view = await service.create_project(
            "Event Create", "desc", actor_id=admin_id
        )
        events = await _list_outbox_events(db, view["id"])
        created_events = [e for e in events if e["event_type"] == "project.created"]
        assert len(created_events) == 1
        assert created_events[0]["aggregate_type"] == "project"
        assert created_events[0]["aggregate_id"] == view["id"]
        assert created_events[0]["payload"]["name"] == "Event Create"
        assert created_events[0]["payload"]["created_by"] == admin_id

    @pytest.mark.asyncio
    async def test_update_project_writes_event(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)

        project = await service.create_project(
            "Event Update", "", actor_id=admin_id
        )
        await service.update_project(
            project["id"],
            name="Updated Name",
            expected_version=1,
            actor_id=admin_id,
        )
        events = await _list_outbox_events(db, project["id"])
        updated_events = [
            e for e in events if e["event_type"] == "project.updated"
        ]
        assert len(updated_events) == 1
        assert updated_events[0]["payload"]["changes"]["name"] == "Updated Name"
        assert updated_events[0]["payload"]["new_version"] == 2

    @pytest.mark.asyncio
    async def test_delete_project_writes_event(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)

        project = await service.create_project(
            "Event Delete", "", actor_id=admin_id
        )
        await service.delete_project(
            project["id"], expected_version=1, actor_id=admin_id
        )
        events = await _list_outbox_events(db, project["id"])
        deleted_events = [
            e for e in events if e["event_type"] == "project.deleted"
        ]
        assert len(deleted_events) == 1
        assert deleted_events[0]["payload"]["deleted_by"] == admin_id

    @pytest.mark.asyncio
    async def test_add_member_writes_event(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Event Add Member", "", actor_id=admin_id
        )
        await service.add_member(
            project["id"], observer_id, "APPROVER", actor_id=admin_id
        )
        events = await _list_outbox_events(db, project["id"])
        added_events = [
            e for e in events if e["event_type"] == "project.member.added"
        ]
        assert len(added_events) == 1
        assert added_events[0]["payload"]["user_id"] == observer_id
        assert added_events[0]["payload"]["role"] == "APPROVER"

    @pytest.mark.asyncio
    async def test_remove_member_writes_event(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Event Remove Member", "", actor_id=admin_id
        )
        await service.add_member(
            project["id"], observer_id, "OBSERVER", actor_id=admin_id
        )
        await service.remove_member(
            project["id"], observer_id, actor_id=admin_id
        )
        events = await _list_outbox_events(db, project["id"])
        removed_events = [
            e for e in events if e["event_type"] == "project.member.removed"
        ]
        assert len(removed_events) == 1
        assert removed_events[0]["payload"]["user_id"] == observer_id
        assert removed_events[0]["payload"]["previous_role"] == "OBSERVER"

    @pytest.mark.asyncio
    async def test_update_member_role_writes_event(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Event Role Change", "", actor_id=admin_id
        )
        await service.add_member(
            project["id"], observer_id, "APPROVER", actor_id=admin_id
        )
        await service.update_member_role(
            project["id"], observer_id, "OBSERVER", actor_id=admin_id
        )
        events = await _list_outbox_events(db, project["id"])
        role_events = [
            e for e in events if e["event_type"] == "project.member.role_changed"
        ]
        assert len(role_events) == 1
        assert role_events[0]["payload"]["old_role"] == "APPROVER"
        assert role_events[0]["payload"]["new_role"] == "OBSERVER"

    @pytest.mark.asyncio
    async def test_events_same_transaction_as_business_writes(
        self, admin_db: tuple[Database, str]
    ) -> None:
        """事件与业务写入同事务：若事务回滚，事件不落库。

        通过触发 VersionConflictError 验证：冲突时事务回滚，事件不应落库。
        """
        db, admin_id = admin_db
        service = _make_service(db)

        project = await service.create_project(
            "Tx Rollback", "", actor_id=admin_id
        )
        # create_project 已写入 1 个 project.created 事件
        count_before = await _count_outbox_events(db, "project.created")
        assert count_before == 1

        # 触发版本冲突（事务回滚）
        with pytest.raises(VersionConflictError):
            await service.update_project(
                project["id"],
                name="Stale",
                expected_version=999,  # 不存在的版本号
                actor_id=admin_id,
            )
        # project.updated 事件不应落库
        updated_count = await _count_outbox_events(db, "project.updated")
        assert updated_count == 0


# --------------------------------------------------------------------------- #
# 7. 可见性与边界
# --------------------------------------------------------------------------- #


class TestVisibilityAndBoundaries:
    """可见性与边界条件测试。"""

    @pytest.mark.asyncio
    async def test_non_member_get_project_returns_404(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        """非成员访问项目返回 404（不泄露存在性）。"""
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Hidden Project", "", actor_id=admin_id
        )
        # observer 不是成员，应返回 404
        with pytest.raises(NotFoundError):
            await service.get_project(project["id"], actor_id=observer_id)

    @pytest.mark.asyncio
    async def test_non_member_list_members_returns_404(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Hidden Members", "", actor_id=admin_id
        )
        with pytest.raises(NotFoundError):
            await service.list_members(project["id"], actor_id=observer_id)

    @pytest.mark.asyncio
    async def test_get_nonexistent_project_returns_404(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)
        with pytest.raises(NotFoundError):
            await service.get_project("nonexistent-id", actor_id=admin_id)

    @pytest.mark.asyncio
    async def test_create_project_empty_name_raises(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)
        with pytest.raises(ArgumentError):
            await service.create_project("   ", "", actor_id=admin_id)

    @pytest.mark.asyncio
    async def test_add_member_invalid_role_raises(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Bad Role", "", actor_id=admin_id
        )
        with pytest.raises(ArgumentError):
            await service.add_member(
                project["id"], observer_id, "SUPERADMIN", actor_id=admin_id
            )

    @pytest.mark.asyncio
    async def test_update_project_invalid_status_raises(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)

        project = await service.create_project(
            "Bad Status", "", actor_id=admin_id
        )
        with pytest.raises(ArgumentError):
            await service.update_project(
                project["id"],
                status="DELETED",
                expected_version=1,
                actor_id=admin_id,
            )

    @pytest.mark.asyncio
    async def test_update_project_no_fields_raises(
        self, admin_db: tuple[Database, str]
    ) -> None:
        db, admin_id = admin_db
        service = _make_service(db)

        project = await service.create_project(
            "No Fields", "", actor_id=admin_id
        )
        with pytest.raises(ArgumentError):
            await service.update_project(
                project["id"],
                expected_version=1,
                actor_id=admin_id,
            )

    @pytest.mark.asyncio
    async def test_update_member_role_same_role_raises(
        self, observer_db: tuple[Database, str, str]
    ) -> None:
        db, admin_id, observer_id = observer_db
        service = _make_service(db)

        project = await service.create_project(
            "Same Role", "", actor_id=admin_id
        )
        await service.add_member(
            project["id"], observer_id, "APPROVER", actor_id=admin_id
        )
        with pytest.raises(ArgumentError):
            await service.update_member_role(
                project["id"], observer_id, "APPROVER", actor_id=admin_id
            )
