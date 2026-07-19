"""Isolated filesystem workspace for non-Git tasks."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from maf_runner.security.boundaries import BoundaryViolation, LocalBoundaryValidator

_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _workspace_root() -> Path:
    return Path(os.environ.get("MAF_WORKSPACE_ROOT", "./data/workspaces")).expanduser().resolve()


def _remove_workspace(path: Path) -> None:
    if path.exists():
        for directory, _children, _files in os.walk(path):
            os.chmod(directory, 0o700)
        shutil.rmtree(path)

async def prepare_generic_workspace(job_id: str, input_bundle_ref: str, writable_subpaths: list[str]) -> str:
    """在受控 root 下创建唯一目录，校验并展开输入 Artifact，只赋予声明子路径写权限。"""
    if not _SAFE_JOB_ID.fullmatch(job_id):
        raise BoundaryViolation("job_id contains unsafe characters")
    root = _workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    validator = LocalBoundaryValidator()
    workspace = Path(validator.require_workspace_path(str(root), job_id))
    if workspace.exists():
        _remove_workspace(workspace)
    workspace.mkdir(mode=0o700)
    inputs = workspace / "inputs"
    # Import into a private staging directory first; tighten permissions after the
    # bundle has been copied so read-only input mounts do not block the import.
    inputs.mkdir(mode=0o700)
    source = Path(input_bundle_ref).expanduser()
    if source.exists():
        if source.is_symlink():
            raise BoundaryViolation("input bundle may not be a symlink")
        source = source.resolve()
        if source.is_dir():
            for item in source.rglob("*"):
                if item.is_symlink():
                    raise BoundaryViolation(f"input bundle contains symlink: {item}")
            shutil.copytree(source, inputs / "bundle", dirs_exist_ok=True)
        elif source.is_file():
            shutil.copy2(source, inputs / source.name)
        else:
            raise BoundaryViolation("input bundle is not a regular file or directory")
    else:
        (inputs / "artifact.ref").write_text(input_bundle_ref, encoding="utf-8")
    os.chmod(inputs, 0o500)
    for relative in writable_subpaths:
        if not relative or Path(relative).is_absolute():
            raise BoundaryViolation(f"invalid writable subpath: {relative!r}")
        target = Path(validator.require_workspace_path(str(workspace), relative))
        target.mkdir(parents=True, exist_ok=True)
    return str(workspace)


async def cleanup_generic_workspace(workspace_path: str) -> None:
    """Remove one workspace idempotently without following escaping links."""
    root = _workspace_root()
    path = Path(LocalBoundaryValidator().require_workspace_path(str(root), workspace_path))
    if path == root:
        raise BoundaryViolation("refusing to remove workspace root")
    if path.exists():
        _remove_workspace(path)


__all__ = ["cleanup_generic_workspace", "prepare_generic_workspace"]
