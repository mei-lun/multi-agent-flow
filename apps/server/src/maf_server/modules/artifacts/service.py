"""Artifact upload, schema/lineage/diff and deterministic validators."""

from __future__ import annotations
import difflib
import hashlib
import io
import json
import re
import uuid
from typing import runtime_checkable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from maf_domain.errors import (
    ArgumentError,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedOperationError,
    VersionConflictError,
    AlreadyExistsError,
)
from maf_server.core.artifact_store import ArtifactFileStore
from maf_server.core.database import Database
from maf_server.core.events import SqliteEventPublisher
from maf_server.core.unit_of_work import SqliteUnitOfWork
from maf_contracts.common import ActorContext
from maf_contracts.events import ActorRef, DomainEvent
from .repository import (
    SqliteArtifactRepository,
    SqliteArtifactSchemaRepository,
    ArtifactRecord,
    ArtifactSchemaRecord,
    ArtifactLineageRecord,
    init_schema,
    new_artifact_id,
)

RESOURCE_REVIEWS = "reviews"
ValidatorStatus = str


@dataclass(frozen=True)
class ValidatorIssue:
    severity: str
    code: str
    message: str
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "path": self.path,
        }


@dataclass(frozen=True)
class ValidatorResult:
    status: str
    issues: list[dict[str, Any]] = field(default_factory=list)
    validator_name: str = ""
    validated_at: str = ""
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "issues": [
                item.to_dict() if isinstance(item, ValidatorIssue) else dict(item)
                for item in self.issues
            ],
            "validator_name": self.validator_name,
            "validated_at": self.validated_at,
            "evidence_refs": list(self.evidence_refs),
        }


def aggregate_review_status(results: list[ValidatorResult | dict[str, Any]]) -> str:
    statuses = {
        str(item.status if isinstance(item, ValidatorResult) else item.get("status"))
        for item in results
    }
    return "ERROR" if "ERROR" in statuses else "FAIL" if "FAIL" in statuses else "PASS"


@runtime_checkable
class Validator(Protocol):
    name: str

    def supports(self, artifact_type: str) -> bool: ...
    async def validate(
        self, artifact: dict[str, Any], context: dict[str, Any]
    ) -> ValidatorResult: ...


class HashIntegrityValidator:
    def __init__(self, *, artifact_types=None, name: str | None = None):
        self.artifact_types = tuple(artifact_types) if artifact_types else None
        self.name = name or "hash_integrity"

    def supports(self, artifact_type: str) -> bool:
        return self.artifact_types is None or artifact_type in self.artifact_types

    async def validate(self, artifact, content):
        expected = artifact.get("content_hash", "")
        actual = hashlib.sha256(content).hexdigest()
        ok = expected == actual
        issues = [] if ok else [ValidatorIssue("ERROR", "hash.mismatch", "content hash mismatch")]
        return ValidatorResult("PASS" if ok else "FAIL", issues, self.name, _now())


class SizeLimitValidator:
    name = "size_limit"

    def __init__(self, max_size_bytes: int, *, artifact_types=None, name: str | None = None):
        if isinstance(max_size_bytes, bool) or max_size_bytes < 0:
            raise ValueError("max_size_bytes must be non-negative")
        self.max_size_bytes = max_size_bytes
        self.artifact_types = tuple(artifact_types) if artifact_types else None
        self.name = name or "size_limit"

    def supports(self, artifact_type: str) -> bool:
        return self.artifact_types is None or artifact_type in self.artifact_types

    async def validate(self, artifact, content):
        issues = []
        if len(content) > self.max_size_bytes:
            issues.append(
                ValidatorIssue(
                    "ERROR", "size.exceeded", f"size {len(content)} exceeds {self.max_size_bytes}"
                )
            )
        if int(artifact.get("size_bytes", len(content))) != len(content):
            issues.append(
                ValidatorIssue("WARNING", "size.metadata_mismatch", "metadata size differs")
            )
        return ValidatorResult(
            "FAIL" if any(i.severity == "ERROR" for i in issues) else "PASS",
            issues,
            self.name,
            _now(),
        )


class JsonSchemaValidator:
    def __init__(
        self, database: Database, schema_name: str, schema_version: int, *, artifact_types=None
    ):
        self.name = f"json_schema:{schema_name}:v{schema_version}"
        self.database = database
        self.schema_name = schema_name
        self.schema_version = schema_version
        self.artifact_types = tuple(artifact_types) if artifact_types else None

    def supports(self, artifact_type: str) -> bool:
        return self.artifact_types is None or artifact_type in self.artifact_types

    async def validate(self, artifact, content):
        try:
            value = json.loads(content)
        except Exception:
            return ValidatorResult(
                "FAIL",
                [ValidatorIssue("ERROR", "json_schema.invalid_json", "invalid JSON", "$")],
                self.name,
                _now(),
            )
        async with self.database.read_connection() as conn:
            async with conn.execute(
                "SELECT json_schema FROM artifact_schemas WHERE schema_name=? AND version=? AND status='ACTIVE'",
                (self.schema_name, self.schema_version),
            ) as c:
                row = await c.fetchone()
        if row is None:
            return ValidatorResult(
                "ERROR",
                [ValidatorIssue("ERROR", "json_schema.not_found", "schema not found")],
                self.name,
                _now(),
            )
        schema = json.loads(row[0])
        issues = []
        try:
            from jsonschema import Draft202012Validator

            validator = Draft202012Validator(schema)
            for error in sorted(validator.iter_errors(value), key=lambda item: list(item.path)):
                path = "$" + "".join(
                    f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.path
                )
                code = (
                    "json_schema.required"
                    if error.validator == "required"
                    else "json_schema.additional_property"
                    if error.validator == "additionalProperties"
                    else "json_schema.violation"
                )
                if error.validator == "required":
                    missing = next(iter(error.validator_value), None)
                    path = f"$.{missing}" if isinstance(missing, str) else path
                elif error.validator == "additionalProperties":
                    import re as _re

                    match = _re.search(r"'([^']+)'", error.message)
                    if match:
                        path = f"$.{match.group(1)}"
                issues.append(ValidatorIssue("ERROR", code, error.message, path))
        except Exception as exc:
            issues.append(ValidatorIssue("ERROR", "json_schema.invalid_schema", str(exc), "$"))
        return ValidatorResult("FAIL" if issues else "PASS", issues, self.name, _now())


class ValidatorRegistry:
    def __init__(
        self, database: Database | None = None, *, file_store=None, permission_service=None
    ):
        self._validators = {}
        self.database = database
        self.file_store = file_store
        self.permission_service = permission_service

    def register(self, validator):
        name = getattr(validator, "name", getattr(validator, "key", validator.__class__.__name__))
        self._validators.setdefault(name, validator)

    def list_validators(self):
        return [self._validators[name] for name in sorted(self._validators)]

    async def validate_artifact(
        self,
        artifact,
        *,
        artifact_type: str | None = None,
        context: dict[str, Any] | bytes | None = None,
        actor_id: str | None = None,
        actor: ActorContext | None = None,
    ) -> list[ValidatorResult]:
        if isinstance(artifact, str):
            if self.database is None or self.file_store is None:
                raise ValueError("database and file_store are required for artifact IDs")
            actor_ctx = _actor(actor_id or "", actor)
            await _require(self.permission_service, actor_ctx, "read", "artifacts")
            artifact_service = ArtifactServiceImpl(
                self.database,
                file_store=self.file_store,
                permission_service=self.permission_service,
            )
            view = await artifact_service.get_artifact(
                artifact, actor_id=actor_id or "", actor=actor_ctx
            )
            content = await artifact_service.download_artifact(
                artifact, actor_id=actor_id or "", actor=actor_ctx
            )
            artifact = view
            context = content
        results = []
        payload = context if isinstance(context, (bytes, bytearray)) else b""
        for name in sorted(self._validators):
            validator = self._validators[name]
            try:
                if validator.supports(artifact_type or artifact.get("artifact_type", "")):
                    results.append(await validator.validate(artifact, payload))
            except Exception as exc:
                results.append(
                    ValidatorResult(
                        "ERROR",
                        [ValidatorIssue("ERROR", "validator.exception", str(exc))],
                        name,
                        _now(),
                    )
                )
        return results

    validate = validate_artifact


def _now():
    return datetime.now(timezone.utc).isoformat()


def _actor(actor_id: str, actor: ActorContext | None):
    return actor or {
        "user_id": actor_id,
        "organization_id": "system",
        "permission_keys": ["ADMIN"],
        "trace_id": "",
    }


async def _require(permission, actor, action, resource):
    if permission is not None:
        await permission.require(actor, action, resource)


class ArtifactServiceImpl:
    def __init__(
        self,
        database: Database,
        *,
        file_store: ArtifactFileStore | None = None,
        store: ArtifactFileStore | None = None,
        permission_service=None,
        repository: SqliteArtifactRepository | None = None,
    ):
        self.database = database
        self.file_store = file_store or store
        self.permission_service = permission_service
        self.repository = repository or SqliteArtifactRepository()
        if self.file_store is None:
            raise ValueError("file_store is required")

    async def upload_artifact(
        self,
        project_id: str,
        artifact_type: str,
        content_bytes: bytes,
        *,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> dict[str, Any]:
        if not isinstance(project_id, str) or not project_id.strip():
            raise ArgumentError("project_id 不能为空")
        if not isinstance(artifact_type, str) or not artifact_type.strip():
            raise ArgumentError("artifact_type 不能为空")
        if not isinstance(content_bytes, (bytes, bytearray)) or not content_bytes:
            raise ArgumentError("content_bytes must not be empty")
        await _require(self.permission_service, _actor(actor_id, actor), "write", "artifacts")
        digest = hashlib.sha256(content_bytes).hexdigest()
        stored = await self.file_store.put_stream(
            io.BytesIO(content_bytes), digest, len(content_bytes)
        )
        aid = new_artifact_id()
        now = _now()
        async with SqliteUnitOfWork(self.database) as uow:
            await self.repository.insert_artifact(
                uow.connection,
                artifact_id=aid,
                project_id=project_id.strip(),
                artifact_type=artifact_type.strip(),
                content_hash=digest,
                storage_key=stored["storage_key"],
                size_bytes=len(content_bytes),
                status="COMPLETED",
                uploaded_by=actor_id,
                uploaded_at=now,
                completed_at=now,
            )
            await SqliteEventPublisher(uow.connection).append(
                DomainEvent(
                    event_type="artifact.uploaded",
                    aggregate_type="artifact",
                    aggregate_id=aid,
                    organization_id="system",
                    project_id=project_id.strip(),
                    actor=ActorRef(actor_type="USER", actor_id=actor_id),
                    trace_id="",
                    payload={"artifact_id": aid, "content_hash": digest},
                )
            )
            await uow.commit()
        return await self.get_artifact(aid, actor_id=actor_id, actor=actor)

    async def get_artifact(self, artifact_id: str, *, actor_id: str, actor=None):
        await _require(self.permission_service, _actor(actor_id, actor), "read", "artifacts")
        async with self.database.read_connection() as conn:
            rec = await self.repository.get_artifact(conn, artifact_id)
        if rec is None or rec.deleted_at:
            raise NotFoundError("artifact 不存在", context={"artifact_id": artifact_id})
        return _view(rec)

    async def list_artifacts(
        self, project_id: str, *, artifact_type: str | None = None, actor_id: str, actor=None
    ):
        await _require(self.permission_service, _actor(actor_id, actor), "read", "artifacts")
        async with self.database.read_connection() as conn:
            records = await self.repository.list_artifacts(
                conn, project_id, artifact_type=artifact_type
            )
        return [_view(rec) for rec in records]

    async def download_artifact(self, artifact_id: str, *, actor_id: str, actor=None) -> bytes:
        view = await self.get_artifact(artifact_id, actor_id=actor_id, actor=actor)
        chunks = []
        async for chunk in self.file_store.open_stream(view["storage_key"]):
            chunks.append(chunk)
        data = b"".join(chunks)
        if hashlib.sha256(data).hexdigest() != view["content_hash"]:
            raise ValueError("内容哈希不符")
        return data

    async def delete_artifact(self, artifact_id: str, *, actor_id: str, actor=None):
        await _require(self.permission_service, _actor(actor_id, actor), "write", "artifacts")
        async with SqliteUnitOfWork(self.database) as uow:
            rec = await self.repository.get_artifact(uow.connection, artifact_id)
            if rec is None:
                await uow.rollback()
                raise NotFoundError("artifact 不存在")
            if rec.deleted_at is not None or rec.status == "DELETED":
                await uow.rollback()
                return _view(rec) | {"status": "DELETED"}
            count = await self.repository.count_by_content_hash(uow.connection, rec.content_hash)
            soft_delete = getattr(self.repository, "soft_delete", None)
            if callable(soft_delete):
                changed = await soft_delete(
                    uow.connection, artifact_id, deleted_at=_now(), expected_version=rec.version_no
                )
            else:
                # Repository doubles and compatible implementations may expose
                # only the public optimistic-locking status transition.
                changed = await self.repository.update_status(
                    uow.connection,
                    artifact_id,
                    status="DELETED",
                    completed_at=rec.completed_at,
                    expected_version=rec.version_no,
                )
            if not changed:
                await uow.rollback()
                raise VersionConflictError("artifact version conflict", retryable=True)
            await SqliteEventPublisher(uow.connection).append(
                DomainEvent(
                    event_type="artifact.deleted",
                    aggregate_type="artifact",
                    aggregate_id=artifact_id,
                    organization_id="system",
                    project_id=rec.project_id,
                    actor=ActorRef(actor_type="USER", actor_id=actor_id),
                    trace_id="",
                    payload={"artifact_id": artifact_id, "content_hash": rec.content_hash},
                )
            )
            await uow.commit()
        if count <= 1:
            await self.file_store.delete_unreferenced(rec.storage_key)
        return _view((await self._record(artifact_id))) | {"status": "DELETED"}

    async def complete_artifact(self, artifact_id: str, *, actor_id: str, actor=None):
        await _require(self.permission_service, _actor(actor_id, actor), "write", "artifacts")
        async with SqliteUnitOfWork(self.database) as uow:
            rec = await self.repository.get_artifact(uow.connection, artifact_id)
            if rec is None:
                await uow.rollback()
                raise NotFoundError("artifact 不存在")
            if rec.status != "UPLOADING":
                await uow.rollback()
                raise UnsupportedOperationError("artifact already completed")
            if not await self.repository.update_status(
                uow.connection,
                artifact_id,
                status="COMPLETED",
                completed_at=_now(),
                expected_version=rec.version_no,
            ):
                await uow.rollback()
                raise VersionConflictError("artifact version conflict", retryable=True)
            await SqliteEventPublisher(uow.connection).append(
                DomainEvent(
                    event_type="artifact.completed",
                    aggregate_type="artifact",
                    aggregate_id=artifact_id,
                    organization_id="system",
                    project_id=rec.project_id,
                    actor=ActorRef(actor_type="USER", actor_id=actor_id),
                    trace_id="",
                    payload={"artifact_id": artifact_id, "content_hash": rec.content_hash},
                )
            )
            await uow.commit()
        return _view(await self._record(artifact_id))

    async def _record(self, aid):
        async with self.database.read_connection() as conn:
            return await self.repository.get_artifact(conn, aid)


class ArtifactSchemaServiceImpl:
    """Artifact schema registry, validation, lineage graph and diff service."""

    def __init__(
        self,
        database: Database,
        *,
        file_store=None,
        permission_service=None,
        schema_repository: SqliteArtifactSchemaRepository | None = None,
    ):
        self.database = database
        self.file_store = file_store
        self._file_store = file_store  # compatibility with repository-injection tests
        self.permission_service = permission_service
        self.schema_repository = schema_repository or SqliteArtifactSchemaRepository()
        self._schema_repository = self.schema_repository
        self.artifact_repository = SqliteArtifactRepository()

    @staticmethod
    def _view_schema(record: ArtifactSchemaRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "schema_name": record.schema_name,
            "version": record.version,
            "json_schema": record.json_schema,
            "status": record.status,
            "created_by": record.created_by,
            "created_at": record.created_at,
            "deprecated_at": record.deprecated_at,
            "version_no": record.version_no,
        }

    async def register_schema(self, name, version, json_schema, *, actor_id, actor=None):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            raise ArgumentError("invalid schema_name")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ArgumentError("invalid schema version")
        if not isinstance(json_schema, dict) or not json_schema.get("type"):
            from maf_domain.errors import ValidationError

            raise ValidationError("invalid JSON schema")
        try:
            from jsonschema import Draft202012Validator

            Draft202012Validator.check_schema(json_schema)
        except Exception as exc:
            from maf_domain.errors import ValidationError

            raise ValidationError("invalid JSON schema") from exc
        actor_ctx = _actor(actor_id, actor)
        await _require(self.permission_service, actor_ctx, "write", "artifact_schemas")
        now = _now()
        record_id = str(uuid.uuid4())
        async with SqliteUnitOfWork(self.database) as uow:
            try:
                await self.schema_repository.insert_schema(
                    uow.connection,
                    schema_id=record_id,
                    schema_name=name,
                    version=version,
                    json_schema=json_schema,
                    created_by=actor_id,
                    created_at=now,
                )
            except Exception as exc:
                await uow.rollback()
                raise AlreadyExistsError("schema already exists") from exc
            await SqliteEventPublisher(uow.connection).append(
                DomainEvent(
                    event_type="artifact.schema_registered",
                    aggregate_type="artifact",
                    aggregate_id=f"{name}:v{version}",
                    organization_id=actor_ctx.get("organization_id", "system"),
                    actor=ActorRef(actor_type="USER", actor_id=actor_id),
                    trace_id=actor_ctx.get("trace_id", ""),
                    payload={"schema_name": name, "version": version, "status": "ACTIVE"},
                )
            )
            await uow.commit()
        return {
            "id": record_id,
            "schema_name": name,
            "version": version,
            "json_schema": json_schema,
            "status": "ACTIVE",
            "created_by": actor_id,
            "created_at": now,
            "deprecated_at": None,
            "version_no": 1,
        }

    async def get_schema(self, name, version, *, actor_id, actor=None):
        await _require(self.permission_service, _actor(actor_id, actor), "read", "artifact_schemas")
        async with self.database.read_connection() as conn:
            record = await self.schema_repository.get_schema(conn, name, version)
        if record is None:
            raise NotFoundError("schema not found")
        return self._view_schema(record)

    async def list_schemas(self, schema_name=None, *, name=None, actor_id, actor=None):
        schema_name = schema_name if schema_name is not None else name
        await _require(self.permission_service, _actor(actor_id, actor), "read", "artifact_schemas")
        async with self.database.read_connection() as conn:
            records = await self.schema_repository.list_schemas(conn, schema_name)
        return [self._view_schema(record) for record in records]

    async def deprecate_schema(self, name, version, *, actor_id, actor=None):
        actor_ctx = _actor(actor_id, actor)
        await _require(self.permission_service, actor_ctx, "write", "artifact_schemas")
        async with SqliteUnitOfWork(self.database) as uow:
            record = await self.schema_repository.get_schema(uow.connection, name, version)
            if record is None:
                await uow.rollback()
                raise NotFoundError("schema not found")
            if record.status != "ACTIVE":
                await uow.rollback()
                raise UnsupportedOperationError("schema already deprecated")
            now = _now()
            changed = await self.schema_repository.deprecate_schema(
                uow.connection,
                name,
                version,
                deprecated_at=now,
                expected_version=record.version_no,
            )
            if not changed:
                await uow.rollback()
                raise VersionConflictError("schema version conflict", retryable=True)
            await SqliteEventPublisher(uow.connection).append(
                DomainEvent(
                    event_type="artifact.schema_deprecated",
                    aggregate_type="artifact",
                    aggregate_id=f"{name}:v{version}",
                    organization_id=actor_ctx.get("organization_id", "system"),
                    actor=ActorRef(actor_type="USER", actor_id=actor_id),
                    trace_id=actor_ctx.get("trace_id", ""),
                    payload={"schema_name": name, "version": version, "status": "DEPRECATED"},
                )
            )
            await uow.commit()
        return await self.get_schema(name, version, actor_id=actor_id, actor=actor)

    async def validate_artifact(
        self, artifact_id, schema_name, schema_version, *, actor_id, actor=None
    ):
        actor_ctx = _actor(actor_id, actor)
        await _require(self.permission_service, actor_ctx, "read", "artifact_schemas")
        schema = await self.get_schema(schema_name, schema_version, actor_id=actor_id, actor=actor)
        artifact_service = ArtifactServiceImpl(
            self.database,
            file_store=self.file_store,
            permission_service=self.permission_service,
        )
        artifact = await artifact_service.get_artifact(artifact_id, actor_id=actor_id, actor=actor)
        content = await artifact_service.download_artifact(
            artifact_id, actor_id=actor_id, actor=actor
        )
        try:
            value = json.loads(content)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return {
                "valid": False,
                "issues": [{"field_path": "$", "message": "invalid JSON"}],
                "schema_name": schema_name,
                "schema_version": schema_version,
                "artifact_id": artifact_id,
            }
        issues: list[dict[str, str]] = []
        from jsonschema import Draft202012Validator

        for error in sorted(
            Draft202012Validator(schema["json_schema"]).iter_errors(value),
            key=lambda item: list(item.path),
        ):
            path = "$" + "".join(
                f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.path
            )
            if error.validator == "required":
                missing = next(iter(error.validator_value), None)
                if isinstance(missing, str):
                    path = f"$.{missing}"
            elif error.validator == "additionalProperties":
                import re as _re

                match = _re.search(r"'([^']+)'", error.message)
                if match:
                    path = f"$.{match.group(1)}"
            issues.append({"field_path": path, "message": error.message})
        return {
            "valid": not issues,
            "issues": issues,
            "schema_name": schema_name,
            "schema_version": schema_version,
            "artifact_id": artifact_id,
            "content_hash": artifact["content_hash"],
        }

    async def record_lineage(
        self,
        artifact_id: str,
        input_artifact_id: str | None = None,
        relation_type: str = "DERIVED_FROM",
        *,
        parent_artifact_ids: list[str] | None = None,
        transformation: str | None = None,
        relation: str | None = None,
        actor_id: str,
        actor=None,
    ):
        actor_ctx = _actor(actor_id, actor)
        await _require(self.permission_service, actor_ctx, "write", "artifacts")
        parents = (
            list(parent_artifact_ids)
            if parent_artifact_ids is not None
            else ([input_artifact_id] if input_artifact_id else [])
        )
        relation = relation or relation_type
        from maf_artifact_schemas.protocol import KNOWN_LINEAGE_RELATIONS

        if not parents:
            raise ArgumentError("parent_artifact_ids must not be empty")
        if relation not in KNOWN_LINEAGE_RELATIONS:
            raise ArgumentError("invalid lineage relation")
        if artifact_id in parents:
            raise ArgumentError("lineage self-cycle")
        if len(set(parents)) != len(parents):
            parents = list(dict.fromkeys(parents))
        now = _now()
        async with SqliteUnitOfWork(self.database) as uow:
            child = await self.artifact_repository.get_artifact(uow.connection, artifact_id)
            if child is None:
                await uow.rollback()
                raise NotFoundError("artifact not found")
            records = []
            for parent_id in parents:
                parent = await self.artifact_repository.get_artifact(uow.connection, parent_id)
                if parent is None:
                    await uow.rollback()
                    raise NotFoundError("parent artifact not found")
                if parent.project_id != child.project_id:
                    await uow.rollback()
                    raise UnsupportedOperationError("跨项目血缘不允许")
                if await self.schema_repository.would_create_cycle(
                    uow.connection, artifact_id=artifact_id, parent_artifact_id=parent_id
                ):
                    await uow.rollback()
                    raise UnsupportedOperationError("血缘成环")
                records.append((parent_id, parent))
            edges = []
            inserted = 0
            for parent_id, _ in records:
                changed = await self.schema_repository.insert_lineage(
                    uow.connection,
                    artifact_id=artifact_id,
                    parent_artifact_id=parent_id,
                    relation=relation,
                    transformation=transformation,
                    recorded_by=actor_id,
                    recorded_at=now,
                )
                inserted += int(changed)
                edges.append(
                    {
                        "artifact_id": artifact_id,
                        "parent_artifact_id": parent_id,
                        "relation": relation,
                        "transformation": transformation,
                        "recorded_by": actor_id,
                        "recorded_at": now,
                    }
                )
            if inserted:
                await SqliteEventPublisher(uow.connection).append(
                    DomainEvent(
                        event_type="artifact.lineage_recorded",
                        aggregate_type="artifact",
                        aggregate_id=artifact_id,
                        organization_id=actor_ctx.get("organization_id", "system"),
                        project_id=child.project_id,
                        actor=ActorRef(actor_type="USER", actor_id=actor_id),
                        trace_id=actor_ctx.get("trace_id", ""),
                        payload={
                            "parent_artifact_ids": parents,
                            "relation": relation,
                            "transformation": transformation,
                            "inserted_count": inserted,
                        },
                    )
                )
            await uow.commit()
        return edges

    @staticmethod
    def _edge_view(edge: ArtifactLineageRecord) -> dict[str, Any]:
        return {
            "artifact_id": edge.artifact_id,
            "parent_artifact_id": edge.parent_artifact_id,
            "relation": edge.relation,
            "transformation": edge.transformation,
            "recorded_by": edge.recorded_by,
            "recorded_at": edge.recorded_at,
        }

    async def _lineage_data(self, artifact_id: str, *, actor_id: str, actor=None):
        await _require(self.permission_service, _actor(actor_id, actor), "read", "artifacts")
        async with self.database.read_connection() as conn:
            artifact = await self.artifact_repository.get_artifact(conn, artifact_id)
            if artifact is None:
                raise NotFoundError("artifact not found")
            records = await self.schema_repository.list_lineage(conn)
        return artifact, records

    async def get_upstream(self, artifact_id: str, *, actor_id: str, actor=None):
        artifact, records = await self._lineage_data(artifact_id, actor_id=actor_id, actor=actor)
        ids = [r.parent_artifact_id for r in records if r.artifact_id == artifact_id]
        async with self.database.read_connection() as conn:
            views = [await self.artifact_repository.get_artifact(conn, item) for item in ids]
        return [_view(item) for item in views if item is not None]

    async def get_downstream(self, artifact_id: str, *, actor_id: str, actor=None):
        artifact, records = await self._lineage_data(artifact_id, actor_id=actor_id, actor=actor)
        ids = [r.artifact_id for r in records if r.parent_artifact_id == artifact_id]
        async with self.database.read_connection() as conn:
            views = [await self.artifact_repository.get_artifact(conn, item) for item in ids]
        return [_view(item) for item in views if item is not None]

    async def get_lineage(self, artifact_id: str, *, actor_id: str, actor=None):
        artifact, records = await self._lineage_data(artifact_id, actor_id=actor_id, actor=actor)
        upstream: set[str] = set()
        frontier = [artifact_id]
        while frontier:
            current = frontier.pop()
            for edge in records:
                if edge.artifact_id == current and edge.parent_artifact_id not in upstream:
                    upstream.add(edge.parent_artifact_id)
                    frontier.append(edge.parent_artifact_id)
        downstream: set[str] = set()
        frontier = [artifact_id]
        while frontier:
            current = frontier.pop()
            for edge in records:
                if edge.parent_artifact_id == current and edge.artifact_id not in downstream:
                    downstream.add(edge.artifact_id)
                    frontier.append(edge.artifact_id)
        async with self.database.read_connection() as conn:
            up_records = [
                await self.artifact_repository.get_artifact(conn, item) for item in sorted(upstream)
            ]
            down_records = [
                await self.artifact_repository.get_artifact(conn, item)
                for item in sorted(downstream)
            ]
        relevant = [
            self._edge_view(edge)
            for edge in records
            if edge.artifact_id in upstream | {artifact_id} | downstream
            and edge.parent_artifact_id in upstream | {artifact_id} | downstream
        ]
        return {
            "artifact_id": artifact_id,
            "upstream": [_view(item) for item in up_records if item is not None],
            "downstream": [_view(item) for item in down_records if item is not None],
            "edges": relevant,
        }

    async def diff_artifacts(
        self,
        left_artifact_id: str,
        right_artifact_id: str,
        *,
        actor_id: str,
        actor=None,
        max_items: int | None = None,
    ):
        await _require(self.permission_service, _actor(actor_id, actor), "read", "artifacts")
        service = ArtifactServiceImpl(
            self.database, file_store=self.file_store, permission_service=self.permission_service
        )
        left_view = await service.get_artifact(left_artifact_id, actor_id=actor_id, actor=actor)
        right_view = await service.get_artifact(right_artifact_id, actor_id=actor_id, actor=actor)
        left = await service.download_artifact(left_artifact_id, actor_id=actor_id, actor=actor)
        right = await service.download_artifact(right_artifact_id, actor_id=actor_id, actor=actor)
        if left_view["content_hash"] == right_view["content_hash"]:
            return {
                "identical": True,
                "diff_kind": "none",
                "entries": [],
                "content_hash_a": left_view["content_hash"],
                "content_hash_b": right_view["content_hash"],
            }
        try:
            a, b = json.loads(left), json.loads(right)
            if not isinstance(a, dict) or not isinstance(b, dict):
                raise ValueError
            entries = []
            for key in sorted(set(a) | set(b)):
                path = f"$.{key}"
                if key not in a:
                    entries.append({"key": path, "type": "added", "after": b[key]})
                elif key not in b:
                    entries.append({"key": path, "type": "removed", "before": a[key]})
                elif a[key] != b[key]:
                    entries.append(
                        {"key": path, "type": "modified", "before": a[key], "after": b[key]}
                    )
            return {
                "identical": False,
                "diff_kind": "field",
                "entries": entries[:max_items] if max_items is not None else entries,
                "content_hash_a": left_view["content_hash"],
                "content_hash_b": right_view["content_hash"],
            }
        except (ValueError, TypeError, json.JSONDecodeError):
            a_lines, b_lines = (
                left.decode(errors="replace").splitlines(),
                right.decode(errors="replace").splitlines(),
            )
            entries = []
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
                None, a_lines, b_lines
            ).get_opcodes():
                if tag == "equal":
                    continue
                if tag == "replace":
                    for old, new in zip(a_lines[i1:i2], b_lines[j1:j2]):
                        entries.append(
                            {
                                "key": f"line:{j1 + 1}",
                                "type": "modified",
                                "before": old,
                                "after": new,
                            }
                        )
                    for index, old in enumerate(
                        a_lines[i1 + len(b_lines[j1:j2]) : i2], i1 + len(b_lines[j1:j2]) + 1
                    ):
                        entries.append({"key": f"line:{index}", "type": "removed", "before": old})
                    for index, new in enumerate(
                        b_lines[j1 + len(a_lines[i1:i2]) : j2], j1 + len(a_lines[i1:i2]) + 1
                    ):
                        entries.append({"key": f"line:{index}", "type": "added", "after": new})
                elif tag == "delete":
                    entries.extend(
                        {"key": f"line:{idx + 1}", "type": "removed", "before": line}
                        for idx, line in enumerate(a_lines[i1:i2], i1)
                    )
                elif tag == "insert":
                    entries.extend(
                        {"key": f"line:{idx + 1}", "type": "added", "after": line}
                        for idx, line in enumerate(b_lines[j1:j2], j1)
                    )
            return {
                "identical": False,
                "diff_kind": "line",
                "entries": entries[:max_items] if max_items is not None else entries,
                "content_hash_a": left_view["content_hash"],
                "content_hash_b": right_view["content_hash"],
            }


def _view(rec: ArtifactRecord | None):
    if rec is None:
        return None
    return {
        "id": rec.id,
        "project_id": rec.project_id,
        "artifact_type": rec.artifact_type,
        "content_hash": rec.content_hash,
        "storage_key": rec.storage_key,
        "size_bytes": rec.size_bytes,
        "status": rec.status,
        "uploaded_by": rec.uploaded_by,
        "uploaded_at": rec.uploaded_at,
        "completed_at": rec.completed_at,
        "deleted_at": rec.deleted_at,
        "version_no": rec.version_no,
    }


__all__ = [
    "ArtifactServiceImpl",
    "ArtifactSchemaServiceImpl",
    "Validator",
    "ValidatorIssue",
    "ValidatorResult",
    "ValidatorRegistry",
    "ValidatorStatus",
    "HashIntegrityValidator",
    "SizeLimitValidator",
    "JsonSchemaValidator",
    "aggregate_review_status",
    "RESOURCE_REVIEWS",
]
