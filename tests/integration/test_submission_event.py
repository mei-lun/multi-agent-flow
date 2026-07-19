from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from maf_contracts.coordination import CoordinationEventModel
from maf_domain.errors import ValidationError
from maf_domain.states import TaskState
from maf_server.gateway.repository.git_cli import ServerGitCli
from maf_server.gateway.repository.service import SubmissionBranchValidator
from maf_server.modules.git_coordination.service import LocalGitCoordinationService


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def submission_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    branch = "maf/task/TASK-001/e2-node-1"
    _git(repo, "switch", "-c", branch)
    (repo / "src").mkdir()
    (repo / "src" / "feature.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "src/feature.py")
    _git(repo, "commit", "-m", "feature")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, branch, base, head


def _task(base: str):
    return {
        "task_id": "TASK-001",
        "status": "IN_PROGRESS",
        "requirements": {"allowed_paths": ["src/**"]},
        "assignment": {
            "node_id": "node-1",
            "assignment_id": "asg-2",
            "assignment_epoch": 2,
            "based_on_control_commit": "c" * 40,
        },
        "delivery": {"base_commit": base},
        "version": 3,
    }


def _event(branch: str, base: str, head: str, **payload_overrides):
    payload = {
        "branch": branch,
        "base_commit": base,
        "head_commit": head,
        "changed_paths": ["src/feature.py"],
        "test_summary": "pytest: 12 passed",
    }
    payload.update(payload_overrides)
    return CoordinationEventModel(
        schema_version=1,
        event_id="evt-submission-0001",
        event_type="SUBMISSION_CREATED",
        node_id="node-1",
        task_id="TASK-001",
        assignment_id="asg-2",
        assignment_epoch=2,
        based_on_control_commit="d" * 40,
        occurred_at="2026-07-19T00:00:00Z",
        payload=payload,
    )


class _Decisions:
    def __init__(self) -> None:
        self.processed: set[tuple[str, str]] = set()
        self.rows: list[tuple] = []

    async def has_processed(self, event_id: str, consumer_id: str) -> bool:
        return (event_id, consumer_id) in self.processed

    async def record_decision(
        self, event_id, consumer_id, decision, result, error, content_hash=""
    ) -> None:
        self.rows.append((event_id, consumer_id, decision, result, error, content_hash))
        if decision == "applied":
            self.processed.add((event_id, consumer_id))


@pytest.mark.asyncio
async def test_legal_submission_enters_submitted_and_is_idempotent(submission_repo) -> None:
    repo, branch, base, head = submission_repo
    cli = ServerGitCli(git_repo_root=repo)
    validator = SubmissionBranchValidator(git_cli=cli, repository_path=str(repo))
    service = LocalGitCoordinationService(git_cli=cli, repository_path=str(repo))
    decisions = _Decisions()
    event = _event(branch, base, head)

    result = await service.process_event(
        event,
        current_epoch=2,
        repository=decisions,
        current_state=TaskState.IN_PROGRESS,
        current_task=_task(base),
        current_control_commit="d" * 40,
        submission_validator=validator,
    )
    assert result.processed is True
    assert result.new_state == "SUBMITTED"
    assert result.new_state != "DONE"

    duplicate = await service.process_event(
        event,
        current_epoch=2,
        repository=decisions,
        current_state=TaskState.IN_PROGRESS,
        current_task=_task(base),
        current_control_commit="d" * 40,
        submission_validator=validator,
    )
    assert duplicate.processed is False
    assert duplicate.decision == "skipped_duplicate"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_head", "wrong_base", "wrong_branch", "wrong_paths"])
async def test_invalid_git_submission_is_rejected(submission_repo, case: str) -> None:
    repo, branch, base, head = submission_repo
    validator = SubmissionBranchValidator(
        git_cli=ServerGitCli(git_repo_root=repo), repository_path=str(repo)
    )
    event = _event(branch, base, head)
    if case == "missing_head":
        event.payload["head_commit"] = "f" * 40
    elif case == "wrong_base":
        event.payload["base_commit"] = head
    elif case == "wrong_branch":
        event.payload["branch"] = "maf/task/TASK-999/e2-node-1"
    else:
        event.payload["changed_paths"] = ["README.md"]

    with pytest.raises(ValidationError):
        await validator.validate_submission(
            event, _task(base), current_control_commit="d" * 40
        )


@pytest.mark.asyncio
async def test_stale_owner_or_epoch_is_rejected_before_git(submission_repo) -> None:
    repo, branch, base, head = submission_repo
    validator = SubmissionBranchValidator(
        git_cli=ServerGitCli(git_repo_root=repo), repository_path=str(repo)
    )
    event = _event(branch, base, head)
    event.assignment_id = "old-assignment"
    with pytest.raises(ValidationError):
        await validator.validate_submission(
            event, _task(base), current_control_commit="d" * 40
        )
