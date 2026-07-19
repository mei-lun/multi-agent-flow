"""Composition root for the server process.

The rest of the server depends on protocols and application services.  This
module is deliberately the only place that chooses concrete implementations.
Importing it is side-effect free: database connections and schema creation are
performed by :meth:`ServerContainer.startup`, which is called from the FastAPI
lifespan handler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from maf_server.config import ServerSettings
from maf_server.core.artifact_store import LocalArtifactFileStore
from maf_server.core.database import Database


@dataclass
class ServerContainer:
    """Long-lived dependencies owned by one ``maf-server`` process.

    ``services`` and ``repositories`` are intentionally mappings.  A mapping
    keeps this composition root tolerant of modules that are still being
    implemented while giving the API router a stable, explicit lookup point.
    The commonly used objects are also exposed as attributes for application
    code and tests.
    """

    settings: ServerSettings
    database: Database
    artifact_store: Any | None = None
    secret_service: Any | None = None
    permission_service: Any | None = None
    repositories: dict[str, Any] = field(default_factory=dict)
    services: dict[str, Any] = field(default_factory=dict)
    routers: dict[str, Any] = field(default_factory=dict)
    _started: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def service(self, name: str) -> Any | None:
        """Return a service by its public composition name."""

        return self.services.get(name)

    @property
    def db(self) -> Database:
        """Backward-friendly short alias for the business database manager."""

        return self.database

    def __getattr__(self, name: str) -> Any:
        """Expose ``<name>_service`` aliases without duplicating state."""

        if name.endswith("_service"):
            value = self.services.get(name.removesuffix("_service"))
            if value is not None:
                return value
        raise AttributeError(name)

    async def startup(self) -> None:
        """Open storage, run migrations, and create idempotent module schemas."""

        if self._started:
            return
        if self._closed:
            raise RuntimeError("ServerContainer 已关闭，不能重新启动")

        await self.database.initialize()
        try:
            # MigrationRunner is synchronous by design and only runs during
            # process startup, before the event loop serves requests.
            from migrations.runner import MigrationRunner

            migrations_dir = Path(__file__).resolve().parents[4] / "migrations"
            MigrationRunner(migrations_dir, self.database.business_db_path).run()
            await self._ensure_module_schemas()
        except BaseException:
            await self.database.close()
            raise
        self._started = True

    async def _ensure_module_schemas(self) -> None:
        """Create schemas supplied by modules that have no numbered migration yet.

        These helpers all use ``CREATE TABLE IF NOT EXISTS`` and are safe to run
        on every startup.  Keeping this work here avoids imports and writes at
        module import time while making a fresh development database usable.
        """

        from maf_server.modules.iam.service import ensure_schema as ensure_iam_schema
        from maf_server.modules.inbox.repository import init_inbox_schema
        from maf_server.modules.model_connections.repository import (
            init_schema as init_model_connection_schema,
        )
        from maf_server.modules.repositories.repository import (
            init_schema as init_repository_schema,
        )
        from maf_server.modules.reviews.service import ensure_reviews_schema
        from maf_server.modules.tools.service import init_tool_registry_schema_on_database

        await ensure_iam_schema(self.database)
        async with self.database.write_connection() as conn:
            await init_repository_schema(conn)
            await init_model_connection_schema(conn)
            await init_inbox_schema(conn)
        await init_tool_registry_schema_on_database(self.database)
        await ensure_reviews_schema(self.database)

    async def close(self) -> None:
        """Close owned resources in reverse composition order; idempotent."""

        if self._closed:
            return
        self._closed = True
        # Current concrete stores are short-lived and do not own connections.
        # Keep the hook generic so a future worker/gateway can register an
        # async ``close`` method without changing the application lifespan.
        for value in reversed(tuple(self.services.values())):
            close = getattr(value, "close", None)
            if close is None:
                continue
            result = close()
            if hasattr(result, "__await__"):
                await result
        await self.database.close()


def _build_secret_service(settings: ServerSettings) -> Any | None:
    """Build the local secret service when an AES master key is configured.

    Keyring remains the preferred backend, but it is optional in a headless
    development environment.  Without a configured master key we leave the
    service unset; callers that need credentials fail closed rather than
    silently persisting plaintext.
    """

    if settings.master_key_file is None:
        return None

    from maf_server.core.secrets import load_master_key
    from maf_server.gateway.secrets.aes_gcm_store import AesGcmFileStore
    from maf_server.gateway.secrets.keyring_store import KeyringStore
    from maf_server.gateway.secrets.local_service import LocalSecretService

    master_key = load_master_key(settings.master_key_file)
    fallback = AesGcmFileStore(
        master_key,
        settings.data_dir / "secrets",
        organization_id=settings.organization_id,
    )
    return LocalSecretService(KeyringStore(), fallback)


def build_container(settings: ServerSettings | None = None) -> ServerContainer:
    """Construct the server dependency graph without opening a database.

    The function is synchronous so it can be called while creating a FastAPI
    application.  I/O is deferred to ``ServerContainer.startup``.  Supplying
    ``settings`` is useful for tests and embedding; otherwise settings are
    loaded from the environment at call time.
    """

    settings = settings or ServerSettings()
    database = Database(settings)
    artifact_store = LocalArtifactFileStore(settings.artifact_root)
    secret_service = _build_secret_service(settings)

    # Imports stay inside the composition function: importing maf_server must
    # not instantiate adapters, read key files, or connect to SQLite.
    from maf_policy import CasbinPermissionService
    from maf_server.gateway.model.adapters import get_default_factory
    from maf_server.gateway.repository.adapter import GitRepositoryAdapter
    from maf_server.gateway.repository.service import LocalRepositoryGateway
    from maf_server.modules.iam.repository import SqliteIamRepository
    from maf_server.modules.iam.service import IamServiceImpl
    from maf_server.modules.inbox.repository import SqliteInboxRepository
    from maf_server.modules.inbox.service import InboxServiceImpl
    from maf_server.modules.model_connections.repository import SqliteModelConnectionRepository
    from maf_server.modules.model_connections.service import ModelConnectionServiceImpl
    from maf_server.modules.projects.repository import SqliteProjectRepository
    from maf_server.modules.projects.service import ProjectApplicationServiceImpl
    from maf_server.modules.repositories.repository import SqliteRepositoryBindingRepository
    from maf_server.modules.repositories.service import RepositoryBindingServiceImpl
    from maf_server.modules.reviews.repository import (
        SqliteArtifactReviewRepository,
        SqliteQualityGateRepository,
    )
    from maf_server.modules.reviews.service import ArtifactReviewServiceImpl, QualityGateServiceImpl
    from maf_server.modules.tools.repository import SqliteToolRegistryRepository
    from maf_server.modules.tools.service import ToolRegistryService
    from maf_server.modules.workflows.service import WorkflowServiceImpl

    permission_service = CasbinPermissionService()
    iam_repository = SqliteIamRepository()
    project_repository = SqliteProjectRepository()
    repository_repository = SqliteRepositoryBindingRepository()

    iam_service = IamServiceImpl(
        database,
        repository=iam_repository,
        permission_service=permission_service,
        secret_service=secret_service,
    )
    project_service = ProjectApplicationServiceImpl(
        database,
        organization_id=settings.organization_id,
        iam_repository=iam_repository,
        project_repository=project_repository,
        permission_service=permission_service,
    )

    # Repository verification uses the local Git CLI and never receives a
    # credential value through the command line.
    repository_gateway = LocalRepositoryGateway(
        git_repo_root=settings.git_repo_root,
        secret_service=secret_service,
        control_branch=settings.control_branch,
    )
    repository_service = RepositoryBindingServiceImpl(
        database,
        adapter=GitRepositoryAdapter(workspace_root=settings.workspace_root),
        secret_service=secret_service,
        organization_id=settings.organization_id,
        iam_repository=iam_repository,
        project_repository=project_repository,
        binding_repository=repository_repository,
        permission_service=permission_service,
    )
    model_service = ModelConnectionServiceImpl(
        database,
        secret_service=secret_service,
        permission_service=permission_service,
        adapter_factory=get_default_factory(),
    )
    tool_service = ToolRegistryService(
        database,
        permission_service=permission_service,
    )
    review_service = ArtifactReviewServiceImpl(
        database,
        permission_service=permission_service,
    )
    gate_service = QualityGateServiceImpl(
        database,
        review_service=review_service,
        permission_service=permission_service,
    )
    inbox_service = InboxServiceImpl(
        database,
        permission_service=permission_service,
        review_service=review_service,
    )
    workflow_service = WorkflowServiceImpl()

    services = {
        "iam": iam_service,
        "projects": project_service,
        "repositories": repository_service,
        "model_connections": model_service,
        "tools": tool_service,
        "reviews": review_service,
        "quality_gates": gate_service,
        "inbox": inbox_service,
        "workflows": workflow_service,
    }
    repositories = {
        "iam": iam_repository,
        "projects": project_repository,
        "repositories": repository_repository,
        "model_connections": SqliteModelConnectionRepository(),
        "inbox": SqliteInboxRepository(),
        "tools": SqliteToolRegistryRepository(),
        "reviews": SqliteArtifactReviewRepository(),
        "quality_gates": SqliteQualityGateRepository(),
    }
    container = ServerContainer(
        settings=settings,
        database=database,
        artifact_store=artifact_store,
        secret_service=secret_service,
        permission_service=permission_service,
        repositories=repositories,
        services=services,
    )
    # Expose the gateway for callers that need repository operations while
    # preserving the service mapping consumed by the API router.
    container.services["repository_gateway"] = repository_gateway
    return container


__all__ = ["ServerContainer", "build_container"]
