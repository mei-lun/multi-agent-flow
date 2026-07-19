from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from maf_domain.errors import IdempotencyConflictError
from maf_server.config import ServerSettings
from maf_server.core.database import Database
from maf_server.modules.git_coordination.repository import SqliteGitCoordinationRepository


def _settings(tmp_path: Path) -> ServerSettings:
    return ServerSettings(
        organization_id="org-test",
        business_db_path=Path("maf.db"),
        checkpointer_db_path=Path("checkpoints.db"),
        artifact_root=Path("artifacts"),
        workspace_root=Path("workspaces"),
        git_repo_root=tmp_path / "repo",
        public_base_url="http://localhost:8000",
        secret_key="projection-test-secret",
        data_dir=tmp_path,
        _env_file=None,
    )


@pytest_asyncio.fixture
async def projection(tmp_path: Path):
    database = Database(_settings(tmp_path))
    await database.initialize()
    repository = SqliteGitCoordinationRepository(database)
    await repository.initialize()
    yield repository, database
    await database.close()


def _snapshot(commit: str, *, status: str = "READY") -> dict[str, Any]:
    return {
        "project_id": "binding-1",
        "control_commit": commit,
        "tasks": [
            {
                "task_id": "TASK-001",
                "status": status,
                "assignment": None,
                "version": 1,
            }
        ],
        "nodes": [
            {
                "node_id": "node-1",
                "status": "ACTIVE",
                "capacity": 1,
                "version": 1,
            }
        ],
        "events": [{"event_id": "evt-1", "event_type": "CLAIM_REQUESTED"}],
        "generated_at": "2026-07-19T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_watermark_advances_only_with_projection_commit(projection) -> None:
    repository, database = projection
    await repository.project_snapshot(_snapshot("a" * 40), None)
    state = await repository.get_projector_state("binding-1")
    assert state is not None
    assert state["projected_control_commit"] == "a" * 40

    broken = _snapshot("b" * 40)
    broken["tasks"][0]["assignment"] = ["invalid"]
    with pytest.raises(Exception):
        await repository.project_snapshot(broken, "a" * 40)

    state = await repository.get_projector_state("binding-1")
    assert state is not None
    assert state["projected_control_commit"] == "a" * 40
    assert await repository.list_projected_tasks("binding-1") == _snapshot("a" * 40)["tasks"]


@pytest.mark.asyncio
async def test_out_of_order_projection_is_rejected(projection) -> None:
    repository, _database = projection
    await repository.project_snapshot(_snapshot("a" * 40), None)
    with pytest.raises(IdempotencyConflictError):
        await repository.project_snapshot(_snapshot("b" * 40), "0" * 40)


@pytest.mark.asyncio
async def test_rebuild_matches_incremental_projection(projection) -> None:
    repository, _database = projection
    first = _snapshot("a" * 40)
    second = _snapshot("b" * 40, status="ASSIGNED")
    second["tasks"][0]["assignment"] = {
        "node_id": "node-1",
        "assignment_epoch": 2,
    }
    await repository.project_snapshot(first, None)
    await repository.project_snapshot(second, "a" * 40)
    incremental = copy.deepcopy(await repository.list_projected_tasks("binding-1"))

    assert await repository.rebuild_projection(second) == "b" * 40
    assert await repository.list_projected_tasks("binding-1") == incremental
    state = await repository.get_projector_state("binding-1")
    assert state is not None and state["projected_control_commit"] == "b" * 40
