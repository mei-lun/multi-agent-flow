from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import aiosqlite

ARTIFACTS_DDL = """
CREATE TABLE IF NOT EXISTS artifacts (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, artifact_type TEXT NOT NULL,
 content_hash TEXT NOT NULL, storage_key TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 status TEXT NOT NULL, uploaded_by TEXT NOT NULL, uploaded_at TEXT NOT NULL,
 completed_at TEXT, deleted_at TEXT, version_no INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_hash ON artifacts(content_hash);
CREATE TABLE IF NOT EXISTS artifact_schemas (
 id TEXT PRIMARY KEY, schema_name TEXT NOT NULL, version INTEGER NOT NULL,
 schema_version INTEGER,
 json_schema TEXT NOT NULL CHECK(json_valid(json_schema)), status TEXT NOT NULL,
 created_by TEXT NOT NULL, created_at TEXT NOT NULL, deprecated_at TEXT,
 version_no INTEGER NOT NULL DEFAULT 1, UNIQUE(schema_name, version)
);
CREATE TABLE IF NOT EXISTS artifact_lineage (
 artifact_id TEXT NOT NULL, parent_artifact_id TEXT NOT NULL,
 relation TEXT NOT NULL, transformation TEXT,
 recorded_by TEXT NOT NULL, recorded_at TEXT NOT NULL,
 PRIMARY KEY(artifact_id, parent_artifact_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_artifact_lineage_parent
 ON artifact_lineage(parent_artifact_id);
"""


async def init_schema(conn: aiosqlite.Connection) -> None:
    for statement in ARTIFACTS_DDL.split(";"):
        if statement.strip():
            await conn.execute(statement)


def new_artifact_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    project_id: str
    artifact_type: str
    content_hash: str
    storage_key: str
    size_bytes: int
    status: str
    uploaded_by: str
    uploaded_at: str
    completed_at: str | None
    deleted_at: str | None
    version_no: int


@dataclass(frozen=True)
class ArtifactSchemaRecord:
    id: str
    schema_name: str
    version: int
    json_schema: dict[str, Any]
    status: str
    created_by: str
    created_at: str
    deprecated_at: str | None
    version_no: int


@dataclass(frozen=True)
class ArtifactLineageRecord:
    artifact_id: str
    parent_artifact_id: str
    relation: str
    transformation: str | None
    recorded_by: str
    recorded_at: str


def _row(row: aiosqlite.Row | tuple | None) -> ArtifactRecord | None:
    if row is None:
        return None
    return ArtifactRecord(
        id=str(row[0]),
        project_id=str(row[1]),
        artifact_type=str(row[2]),
        content_hash=str(row[3]),
        storage_key=str(row[4]),
        size_bytes=int(row[5]),
        status=str(row[6]),
        uploaded_by=str(row[7]),
        uploaded_at=str(row[8]),
        completed_at=row[9],
        deleted_at=row[10],
        version_no=int(row[11]),
    )


class SqliteArtifactRepository:
    columns = "id, project_id, artifact_type, content_hash, storage_key, size_bytes, status, uploaded_by, uploaded_at, completed_at, deleted_at, version_no"

    async def insert_artifact(self, conn, **kwargs) -> None:
        await conn.execute(
            "INSERT INTO artifacts(id, project_id, artifact_type, content_hash, storage_key, size_bytes, status, uploaded_by, uploaded_at, completed_at, version_no) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kwargs["artifact_id"],
                kwargs["project_id"],
                kwargs["artifact_type"],
                kwargs["content_hash"],
                kwargs["storage_key"],
                kwargs["size_bytes"],
                kwargs["status"],
                kwargs["uploaded_by"],
                kwargs["uploaded_at"],
                kwargs.get("completed_at"),
                kwargs.get("version_no", 1),
            ),
        )

    async def get_artifact(self, conn, artifact_id: str) -> ArtifactRecord | None:
        async with conn.execute(
            f"SELECT {self.columns} FROM artifacts WHERE id = ?", (artifact_id,)
        ) as cursor:
            return _row(await cursor.fetchone())

    async def list_artifacts(
        self, conn, project_id: str, *, artifact_type: str | None = None
    ) -> list[ArtifactRecord]:
        sql = f"SELECT {self.columns} FROM artifacts WHERE project_id = ? AND deleted_at IS NULL"
        args: list[Any] = [project_id]
        if artifact_type:
            sql += " AND artifact_type = ?"
            args.append(artifact_type)
        sql += " ORDER BY uploaded_at, id"
        async with conn.execute(sql, args) as cursor:
            return [record for record in (_row(row) for row in await cursor.fetchall()) if record]

    async def update_status(
        self,
        conn,
        artifact_id: str,
        *,
        status: str,
        completed_at: str | None,
        expected_version: int,
    ) -> int:
        cursor = await conn.execute(
            "UPDATE artifacts SET status = ?, completed_at = ?, version_no = version_no + 1 WHERE id = ? AND version_no = ?",
            (status, completed_at, artifact_id, expected_version),
        )
        return cursor.rowcount

    async def soft_delete(
        self, conn, artifact_id: str, *, deleted_at: str, expected_version: int
    ) -> int:
        cursor = await conn.execute(
            "UPDATE artifacts SET status = 'DELETED', deleted_at = ?, version_no = version_no + 1 WHERE id = ? AND version_no = ? AND deleted_at IS NULL",
            (deleted_at, artifact_id, expected_version),
        )
        return cursor.rowcount

    async def count_by_content_hash(self, conn, content_hash: str) -> int:
        async with conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE content_hash = ? AND deleted_at IS NULL",
            (content_hash,),
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else 0

    async def get_schema(self, conn, name: str, version: int):
        async with conn.execute(
            "SELECT id, schema_name, schema_version, json_schema, status, created_by, created_at, deprecated_at, version_no FROM artifact_schemas WHERE schema_name = ? AND schema_version = ?",
            (name, version),
        ) as cursor:
            return await cursor.fetchone()


def _schema_row(row: aiosqlite.Row | tuple | None) -> ArtifactSchemaRecord | None:
    if row is None:
        return None
    return ArtifactSchemaRecord(
        id=str(row[0]),
        schema_name=str(row[1]),
        version=int(row[2]),
        json_schema=json.loads(str(row[3])),
        status=str(row[4]),
        created_by=str(row[5]),
        created_at=str(row[6]),
        deprecated_at=row[7],
        version_no=int(row[8]),
    )


def _lineage_row(row: aiosqlite.Row | tuple) -> ArtifactLineageRecord:
    return ArtifactLineageRecord(
        artifact_id=str(row[0]),
        parent_artifact_id=str(row[1]),
        relation=str(row[2]),
        transformation=row[3],
        recorded_by=str(row[4]),
        recorded_at=str(row[5]),
    )


class SqliteArtifactSchemaRepository:
    """Schema and lineage metadata operations bound to a caller-owned transaction."""

    _schema_columns = (
        "id, schema_name, version, json_schema, status, created_by, "
        "created_at, deprecated_at, version_no"
    )
    _lineage_columns = (
        "artifact_id, parent_artifact_id, relation, transformation, recorded_by, recorded_at"
    )

    async def insert_schema(
        self,
        conn: aiosqlite.Connection,
        *,
        schema_id: str,
        schema_name: str,
        version: int,
        json_schema: dict[str, Any],
        created_by: str,
        created_at: str,
    ) -> None:
        await conn.execute(
            "INSERT INTO artifact_schemas "
            "(id, schema_name, version, schema_version, json_schema, status, "
            "created_by, created_at, version_no) "
            "VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, 1)",
            (
                schema_id,
                schema_name,
                version,
                version,
                json.dumps(json_schema, ensure_ascii=False, sort_keys=True),
                created_by,
                created_at,
            ),
        )

    async def get_schema(
        self, conn: aiosqlite.Connection, name: str, version: int
    ) -> ArtifactSchemaRecord | None:
        async with conn.execute(
            f"SELECT {self._schema_columns} FROM artifact_schemas "
            "WHERE schema_name = ? AND version = ?",
            (name, version),
        ) as cursor:
            return _schema_row(await cursor.fetchone())

    async def list_schemas(
        self, conn: aiosqlite.Connection, schema_name: str | None = None
    ) -> list[ArtifactSchemaRecord]:
        sql = f"SELECT {self._schema_columns} FROM artifact_schemas"
        params: list[object] = []
        if schema_name is not None:
            sql += " WHERE schema_name = ?"
            params.append(schema_name)
        sql += " ORDER BY schema_name, version"
        async with conn.execute(sql, params) as cursor:
            return [
                record
                for row in await cursor.fetchall()
                if (record := _schema_row(row)) is not None
            ]

    async def deprecate_schema(
        self,
        conn: aiosqlite.Connection,
        name: str,
        version: int,
        *,
        deprecated_at: str,
        expected_version: int,
    ) -> int:
        cursor = await conn.execute(
            "UPDATE artifact_schemas SET status = 'DEPRECATED', deprecated_at = ?, "
            "version_no = version_no + 1 "
            "WHERE schema_name = ? AND version = ? AND status = 'ACTIVE' "
            "AND version_no = ?",
            (deprecated_at, name, version, expected_version),
        )
        return cursor.rowcount

    async def insert_lineage(
        self,
        conn: aiosqlite.Connection,
        *,
        artifact_id: str,
        parent_artifact_id: str,
        relation: str,
        transformation: str | None,
        recorded_by: str,
        recorded_at: str,
    ) -> bool:
        cursor = await conn.execute(
            "INSERT OR IGNORE INTO artifact_lineage "
            "(artifact_id, parent_artifact_id, relation, transformation, "
            "recorded_by, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                artifact_id,
                parent_artifact_id,
                relation,
                transformation,
                recorded_by,
                recorded_at,
            ),
        )
        return cursor.rowcount == 1

    async def list_lineage(self, conn: aiosqlite.Connection) -> list[ArtifactLineageRecord]:
        async with conn.execute(
            f"SELECT {self._lineage_columns} FROM artifact_lineage "
            "ORDER BY artifact_id, parent_artifact_id, relation"
        ) as cursor:
            return [_lineage_row(row) for row in await cursor.fetchall()]

    async def would_create_cycle(
        self,
        conn: aiosqlite.Connection,
        *,
        artifact_id: str,
        parent_artifact_id: str,
    ) -> bool:
        async with conn.execute(
            "WITH RECURSIVE ancestors(id) AS ("
            " SELECT parent_artifact_id FROM artifact_lineage WHERE artifact_id = ?"
            " UNION"
            " SELECT l.parent_artifact_id FROM artifact_lineage l"
            " JOIN ancestors a ON l.artifact_id = a.id"
            ") SELECT 1 FROM ancestors WHERE id = ? LIMIT 1",
            (parent_artifact_id, artifact_id),
        ) as cursor:
            return await cursor.fetchone() is not None


__all__ = [
    "ARTIFACTS_DDL",
    "ArtifactLineageRecord",
    "ArtifactRecord",
    "ArtifactSchemaRecord",
    "SqliteArtifactRepository",
    "SqliteArtifactSchemaRepository",
    "init_schema",
    "new_artifact_id",
]
