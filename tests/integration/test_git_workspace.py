import os
import subprocess
from pathlib import Path

import pytest

from maf_repository_adapters import SubprocessGitCli
from maf_runner.security.boundaries import BoundaryViolation
from maf_runner.workspace.git import LocalGitWorkspace


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.test", "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.test"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.asyncio
async def test_git_workspace_isolated_branch_and_collect(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    (source / "README.md").write_text("base", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "base")
    base = _git(source, "rev-parse", "HEAD")
    tree = _git(source, "rev-parse", "HEAD^{tree}")
    cli = SubprocessGitCli(allowed_roots=[tmp_path])
    manager = LocalGitWorkspace(git_cli=cli, workspace_root=tmp_path / "work", node_id="node-a", assignment_epoch=2)
    workspace = Path(await manager.prepare("TASK-1", str(source), base, tree, ["outputs"]))
    (workspace / "outputs").mkdir()
    (workspace / "outputs" / "result.txt").write_text("done", encoding="utf-8")
    result = await manager.collect(str(workspace))
    assert result["branch"] == "maf/task/TASK-1/2/node-a"
    assert result["changed_paths"] == ["outputs/result.txt"]
    assert _git(source, "status", "--porcelain") == ""
    await manager.cleanup(str(workspace))
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_git_workspace_rejects_tree_mismatch(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    (source / "a").write_text("a", encoding="utf-8")
    _git(source, "add", "a")
    _git(source, "commit", "-m", "base")
    base = _git(source, "rev-parse", "HEAD")
    manager = LocalGitWorkspace(git_cli=SubprocessGitCli(allowed_roots=[tmp_path]), workspace_root=tmp_path / "work", node_id="node-a", assignment_epoch=1)
    with pytest.raises(BoundaryViolation):
        await manager.prepare("TASK-1", str(source), base, "0" * 40, ["outputs"])
