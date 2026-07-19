"""仓库绑定持久化接口与 SQLite 实现。

TASK-035 范围：
- ``PROJECT_REPOSITORIES_DDL`` 与 ``init_schema``：``project_repositories`` 表 DDL
  与幂等建表函数（供测试与首次启动使用；正式部署由 migrations 负责）。
- ``RepositoryBindingRecord``：行映射 dataclass，不含明文凭据。
- ``SqliteRepositoryBindingRepository``：CRUD 方法，接受 ``aiosqlite.Connection``，
  不自开事务，由 service 层负责 ``BEGIN IMMEDIATE``/``COMMIT``。

保留 ``RepositoryStateRepository`` Protocol（TASK-083+ 接口契约）。

表结构遵循设计文档：
- ``id``：绑定 ID（UUID4）。
- ``project_id``：所属项目 ID。
- ``repository_url``：仓库 URL（不含凭据）。
- ``branch``：绑定的 base branch。
- ``credential_type``：凭据方式（HTTPS_TOKEN/SSH_KEY/NONE）。
- ``credential_secret_id``：SecretService 引用（HTTPS_TOKEN 模式），不存明文。
- ``ssh_key_path``：SSH 私钥路径（SSH_KEY 模式），路径本身不是密钥。
- ``verified``/``verified_at``：验证状态与时间。
- ``bound_by``/``bound_at``：绑定操作者与时间。
- ``version_no``：乐观锁版本号。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import aiosqlite

from .schemas import CredentialType, RepositoryChangeView, RepositoryHealth

# --------------------------------------------------------------------------- #
# TASK-035: project_repositories 表 DDL
# --------------------------------------------------------------------------- #

PROJECT_REPOSITORIES_DDL: str = """
CREATE TABLE IF NOT EXISTS project_repositories (
    id                    TEXT    PRIMARY KEY,
    project_id            TEXT    NOT NULL,
    repository_url        TEXT    NOT NULL,
    branch                TEXT    NOT NULL,
    credential_type       TEXT    NOT NULL DEFAULT 'HTTPS_TOKEN',
    credential_secret_id  TEXT,
    ssh_key_path          TEXT,
    verified              INTEGER NOT NULL DEFAULT 0,
    verified_at           TEXT,
    bound_by              TEXT    NOT NULL,
    bound_at              TEXT    NOT NULL,
    version_no            INTEGER NOT NULL DEFAULT 1,
    CHECK (credential_type IN ('HTTPS_TOKEN', 'SSH_KEY', 'NONE'))
);

CREATE INDEX IF NOT EXISTS idx_project_repositories_project_id
    ON project_repositories(project_id);
"""

_BINDING_COLUMNS = (
    "id, project_id, repository_url, branch, credential_type, "
    "credential_secret_id, ssh_key_path, verified, verified_at, "
    "bound_by, bound_at, version_no"
)


async def init_schema(conn: aiosqlite.Connection) -> None:
    """在给定连接上创建 ``project_repositories`` 表（幂等）。

    正式部署由 ``migrations/`` 顺序迁移负责；本函数供测试与开发期首次启动使用。
    """
    for raw in PROJECT_REPOSITORIES_DDL.split(";"):
        stmt = raw.strip()
        if stmt:
            await conn.execute(stmt)


# --------------------------------------------------------------------------- #
# TASK-035: 行映射 dataclass
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RepositoryBindingRecord:
    """``project_repositories`` 表行映射。

    ``credential_secret_id`` 引用 SecretStore 中的 HTTPS token；``ssh_key_path``
    指向受控 SSH 私钥文件。两者互斥：HTTPS 绑定用 ``credential_secret_id``，
    SSH 绑定用 ``ssh_key_path``，NONE 绑定两者皆空。明文绝不进入本结构。
    """

    id: str
    project_id: str
    repository_url: str
    branch: str
    credential_type: CredentialType
    credential_secret_id: str | None = None
    ssh_key_path: str | None = None
    verified: bool = False
    verified_at: str | None = None
    bound_by: str = ""
    bound_at: str = ""
    version_no: int = 1


def _row_to_binding(row: aiosqlite.Row | tuple | None) -> RepositoryBindingRecord | None:
    """把 ``project_repositories`` 表行映射为 ``RepositoryBindingRecord``。"""
    if row is None:
        return None
    if isinstance(row, aiosqlite.Row):
        row = tuple(row)
    return RepositoryBindingRecord(
        id=str(row[0]),
        project_id=str(row[1]),
        repository_url=str(row[2]),
        branch=str(row[3]),
        credential_type=str(row[4]),  # type: ignore[arg-type]
        credential_secret_id=str(row[5]) if row[5] is not None else None,
        ssh_key_path=str(row[6]) if row[6] is not None else None,
        verified=bool(row[7]),
        verified_at=str(row[8]) if row[8] is not None else None,
        bound_by=str(row[9]),
        bound_at=str(row[10]),
        version_no=int(row[11]),
    )


def binding_record_to_view(record: RepositoryBindingRecord) -> dict:
    """``RepositoryBindingRecord`` → 对外视图 dict（不暴露 secret_id/key_path）。"""
    credential_configured = (
        record.credential_secret_id is not None
        or record.ssh_key_path is not None
    )
    return {
        "id": record.id,
        "project_id": record.project_id,
        "repository_url": record.repository_url,
        "branch": record.branch,
        "credential_type": record.credential_type,
        "credential_configured": credential_configured,
        "verified": record.verified,
        "verified_at": record.verified_at,
        "bound_by": record.bound_by,
        "bound_at": record.bound_at,
        "version": record.version_no,
    }


# --------------------------------------------------------------------------- #
# TASK-035: SQLite 具体实现
# --------------------------------------------------------------------------- #


class SqliteRepositoryBindingRepository:
    """``project_repositories`` 表的 SQLite CRUD 实现。

    所有方法接受 ``aiosqlite.Connection``（由 ``SqliteUnitOfWork`` 提供），
    不自开事务，由 service 层负责事务边界。
    """

    async def get_binding(
        self, conn: aiosqlite.Connection, binding_id: str
    ) -> RepositoryBindingRecord | None:
        """按 ID 读取绑定记录；不存在返回 ``None``。"""
        sql = f"SELECT {_BINDING_COLUMNS} FROM project_repositories WHERE id = ? LIMIT 1"
        async with conn.execute(sql, (binding_id,)) as cur:
            row = await cur.fetchone()
        return _row_to_binding(row)

    async def list_by_project(
        self, conn: aiosqlite.Connection, project_id: str
    ) -> list[RepositoryBindingRecord]:
        """返回项目的全部绑定，按 ``bound_at`` 升序。"""
        sql = (
            f"SELECT {_BINDING_COLUMNS} FROM project_repositories "
            "WHERE project_id = ? ORDER BY bound_at ASC, id ASC"
        )
        async with conn.execute(sql, (project_id,)) as cur:
            rows = await cur.fetchall()
        return [r for r in (_row_to_binding(row) for row in rows) if r is not None]

    async def insert_binding(
        self, conn: aiosqlite.Connection, record: RepositoryBindingRecord
    ) -> None:
        """插入新绑定行；``version_no`` 初始为 1。"""
        await conn.execute(
            "INSERT INTO project_repositories (id, project_id, repository_url, branch, "
            "credential_type, credential_secret_id, ssh_key_path, verified, verified_at, "
            "bound_by, bound_at, version_no) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.project_id,
                record.repository_url,
                record.branch,
                record.credential_type,
                record.credential_secret_id,
                record.ssh_key_path,
                1 if record.verified else 0,
                record.verified_at,
                record.bound_by,
                record.bound_at,
                record.version_no,
            ),
        )

    async def delete_binding(
        self, conn: aiosqlite.Connection, binding_id: str
    ) -> None:
        """物理删除绑定行（绑定关系不可恢复）。"""
        await conn.execute(
            "DELETE FROM project_repositories WHERE id = ?",
            (binding_id,),
        )


# --------------------------------------------------------------------------- #
# TASK-083+ 占位 Protocol（保留，本任务不修改）
# --------------------------------------------------------------------------- #


class RepositoryStateRepository(Protocol):
    async def get_binding_record(self, binding_id: str) -> dict | None:
        """返回 Gateway 所需位置/base/secret reference；不存在为 None。"""
        ...

    async def save_health(self, binding_id: str, health: RepositoryHealth) -> RepositoryHealth:
        """更新最近验证状态和固定 base commit，不写远端响应原文。"""
        ...

    async def get_change(self, change_id: str) -> RepositoryChangeView | None:
        """按 change ID 返回 PR/本地 Review 投影。"""
        ...

    async def get_change_by_run(self, run_id: str) -> RepositoryChangeView | None:
        """返回 Run 唯一 RepositoryChange；无代码仓库 Run 返回 None。"""
        ...

    async def save_change(self, item: RepositoryChangeView, expected_version: int | None) -> RepositoryChangeView:
        """乐观锁保存外部状态投影；不执行任何 Git/GitHub 动作。"""
        ...


__all__ = [
    "PROJECT_REPOSITORIES_DDL",
    "init_schema",
    "RepositoryBindingRecord",
    "SqliteRepositoryBindingRepository",
    "binding_record_to_view",
    "RepositoryStateRepository",
]
