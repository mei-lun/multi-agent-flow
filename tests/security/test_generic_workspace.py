from pathlib import Path

import pytest

from maf_runner.security.boundaries import BoundaryViolation
from maf_runner.workspace.generic import cleanup_generic_workspace, prepare_generic_workspace


@pytest.mark.asyncio
async def test_generic_workspaces_are_isolated_and_cleanup_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("MAF_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    source = tmp_path / "input.txt"
    source.write_text("input", encoding="utf-8")
    first = Path(await prepare_generic_workspace("job-1", str(source), ["outputs"] ))
    second = Path(await prepare_generic_workspace("job-2", str(source), ["outputs"] ))
    assert first != second
    assert (first / "outputs").is_dir()
    await cleanup_generic_workspace(str(first))
    await cleanup_generic_workspace(str(first))
    assert not first.exists()


@pytest.mark.asyncio
async def test_generic_workspace_rejects_escape_and_symlink(tmp_path, monkeypatch):
    monkeypatch.setenv("MAF_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    with pytest.raises(BoundaryViolation):
        await prepare_generic_workspace("../escape", "opaque", ["outputs"])
    source = tmp_path / "source"
    source.mkdir()
    (source / "link").symlink_to(tmp_path)
    with pytest.raises(BoundaryViolation):
        await prepare_generic_workspace("job-1", str(source), ["outputs"])
