"""Read exact, role-bound Skill versions from a checked-out Git snapshot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any


class SkillClient:
    """Hash-verifying, path-confined reader for Git-distributed Skill files."""

    def __init__(self, skill_root: Path, bound_version_ids: set[str]) -> None:
        self._root = skill_root.resolve()
        self._bound = frozenset(bound_version_ids)

    def read_file(self, version_id: str, path: str) -> bytes:
        if version_id not in self._bound:
            raise PermissionError("skill version is not bound to this Role snapshot")
        relative = _normalize_relative(path)
        version_root = (self._root / version_id).resolve()
        _ensure_within(version_root, self._root)
        index_path = version_root / "index.json"
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("invalid skill index") from exc
        if index.get("version_id") != version_id:
            raise ValueError("skill index version mismatch")
        entries = {
            item.get("path"): item
            for item in index.get("files", [])
            if isinstance(item, dict)
        }
        item = entries.get(relative)
        if item is None:
            raise FileNotFoundError(relative)
        target = (version_root / "files" / relative).resolve()
        _ensure_within(target, version_root / "files")
        content = target.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != item.get("sha256") or len(content) != item.get("size"):
            raise ValueError("skill file hash or size mismatch")
        return content


def _normalize_relative(path: str) -> str:
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
        raise ValueError("invalid skill path")
    value = PurePosixPath(path)
    if value.is_absolute() or any(part in {"", ".", ".."} for part in value.parts):
        raise ValueError("skill path escapes version root")
    return value.as_posix()


def _ensure_within(path: Path, root: Path) -> None:
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("skill path escapes repository root") from exc


__all__ = ["SkillClient"]
