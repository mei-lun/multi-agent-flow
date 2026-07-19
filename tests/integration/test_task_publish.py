"""TASK-017 task publication and control-head fencing tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from maf_domain.errors import UnsupportedOperationError, ValidationError
from maf_repository_adapters import SubprocessGitCli
from maf_server.git_coordination.schemas import SchemaLoader
from maf_server.modules.git_coordination.service import LocalGitCoordinationService

from tests.fixtures.git_repo import init_local_git_repo


def _run(value):
    return asyncio.run(value)


def _task(task_id: str, *, key: str, dependencies: list[str] | None = None) -> dict:
    return {
        "schema_version": 1,
        "task_id": task_id,
        "title": task_id,
        "status": "READY",
        "requirements": {"idempotency_key": key},
        "dependencies": dependencies or [],
        "assignment": None,
        "progress": {},
        "delivery": {},
        "version": 1,
    }


@pytest.fixture()
def service(tmp_path: Path):
    repo = init_local_git_repo(tmp_path / "repo").path
    cli = SubprocessGitCli(allowed_roots=[repo])
    instance = LocalGitCoordinationService(
        git_cli=cli,
        repository_path=str(repo),
        templates_dir=Path(__file__).resolve().parents[2] / "templates" / "git_coordination",
        schema_loader=SchemaLoader(),
    )
    _run(instance.initialize_project("binding-1", "proj-001"))
    return instance


def test_publish_is_idempotent_and_snapshot_contains_task(service):
    head = _run(service.fetch_control("proj-001"))["control_commit"]
    task = _task("TASK-001", key="run-1:node-1")
    first = _run(service.publish_tasks("proj-001", [task], head))
    second = _run(service.publish_tasks("proj-001", [task], head))
    assert first == second
    snapshot = _run(service.fetch_control("proj-001"))
    assert [item["task_id"] for item in snapshot["tasks"]] == ["TASK-001"]


def test_publish_rejects_missing_dependency_cycle_and_stale_head(service):
    head = _run(service.fetch_control("proj-001"))["control_commit"]
    with pytest.raises(ValidationError):
        _run(
            service.publish_tasks(
                "proj-001",
                [_task("TASK-001", key="a", dependencies=["TASK-002"]), _task("TASK-002", key="b", dependencies=["TASK-001"])],
                head,
            )
        )
    with pytest.raises(UnsupportedOperationError):
        _run(service.publish_tasks("proj-001", [_task("TASK-003", key="c")], "0" * 40))
