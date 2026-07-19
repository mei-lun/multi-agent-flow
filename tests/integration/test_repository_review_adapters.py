"""TASK-083～086 controlled local/GitHub review adapter tests."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from maf_server.gateway.repository.github import GitHubReviewAdapter
from maf_server.gateway.repository.local_git import LocalGitReviewAdapter
from maf_server.gateway.repository.merge_gate import MergeGate


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def test_local_review_is_idempotent_and_fenced(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    adapter = LocalGitReviewAdapter(repo, workspace_root=tmp_path / "review")

    async def run():
        result = await adapter.materialize_change({"run_id": "run-1", "base_commit": base, "work_branch": "maf/integration/run-1"})
        review = await adapter.open_review({"idempotency_key": "run-1", "base_commit": base, "head_commit": result["head_commit"]})
        assert await adapter.open_review({"idempotency_key": "run-1", "base_commit": base, "head_commit": "changed"}) == review
        review.update({"code_review": "PASS", "tests": "PASS", "product_acceptance": "APPROVED", "inbox": "APPROVE", "checks": "PASS", "approvals": 1})
        blocked = await MergeGate().merge(adapter, {"idempotency_key": "run-1", "expected_head_commit": "different"}, review)
        assert blocked["status"] == "CONFLICTED"
        await adapter.cleanup(result["workspace_path"])

    asyncio.run(run())


class _GitHub:
    def __init__(self) -> None:
        self.created = 0

    async def create_pull_request(self, owner, repo, head, base, title, body):
        self.created += 1
        return {"number": 7, "head": head, "body": body}

    async def get_pull_request(self, owner, repo, number):
        return {"number": number, "head": "head-1", "mergeable": True}

    async def merge_pull_request(self, owner, repo, number, expected_head, method):
        return {"status": "MERGED", "merge_commit": "merge-1"}


def test_github_retry_uses_marker_once() -> None:
    async def run():
        client = _GitHub()
        adapter = GitHubReviewAdapter(client)
        await adapter.open_review(owner="o", repo="r", head="h", base="main", title="t", body="b", marker="run-1")
        await adapter.open_review(owner="o", repo="r", head="h", base="main", title="t", body="b", marker="run-1")
        assert client.created == 1

    asyncio.run(run())
