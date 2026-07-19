"""Secure output discovery, validation and content hashing."""

import hashlib
import json
from pathlib import Path
from typing import Protocol

from maf_runner.security.boundaries import BoundaryViolation, LocalBoundaryValidator


class ArtifactPackager(Protocol):
    async def package_outputs(self, workspace_path: str, output_contract: dict, declared_paths: list[str]) -> dict:
        """只登记声明且位于 workspace 内的路径；阻止链接/逃逸，限制数量/大小并计算哈希。

        小型代码、文档和报告随任务分支提交；超过普通 Git 阈值的文件只有仓库已配置 Git LFS
        才允许。返回路径/hash/size manifest，Schema 失败的文件不得列为成功输出。
        """
        ...


class LocalArtifactPackager:
    def __init__(self, *, max_files: int = 1000, max_bytes: int = 100 * 1024 * 1024) -> None:
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._boundary = LocalBoundaryValidator()

    @staticmethod
    def _schema_check(value, schema: dict, path: str) -> None:
        expected = schema.get("type")
        if expected == "object" and not isinstance(value, dict):
            raise BoundaryViolation(f"output {path} must contain a JSON object")
        if expected == "array" and not isinstance(value, list):
            raise BoundaryViolation(f"output {path} must contain a JSON array")
        if isinstance(value, dict):
            missing = [str(key) for key in schema.get("required", []) if key not in value]
            if missing:
                raise BoundaryViolation(f"output {path} is missing fields: {missing}")

    async def package_outputs(
        self,
        workspace_path: str,
        output_contract: dict,
        declared_paths: list[str],
    ) -> dict:
        root = Path(workspace_path).resolve()
        if not root.is_dir():
            raise BoundaryViolation("workspace path does not exist")
        if not isinstance(declared_paths, list):
            raise BoundaryViolation("declared output paths must be a list")
        entries: list[dict] = []
        total = 0
        seen: set[str] = set()
        schemas = output_contract.get("path_schemas", {}) if isinstance(output_contract, dict) else {}
        for declared in declared_paths:
            path = Path(self._boundary.require_workspace_path(str(root), declared))
            if path.is_symlink() or not path.is_file():
                raise BoundaryViolation(f"declared output is not a regular file: {declared}")
            relative = path.relative_to(root).as_posix()
            if relative in seen:
                continue
            seen.add(relative)
            size = path.stat().st_size
            total += size
            self._boundary.require_output_limits(
                len(seen), total,
                {"max_output_files": self._max_files, "max_output_bytes": self._max_bytes},
            )
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(64 * 1024):
                    digest.update(chunk)
            schema = schemas.get(relative) if isinstance(schemas, dict) else None
            if isinstance(schema, dict):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BoundaryViolation(f"output {relative} is not valid JSON") from exc
                self._schema_check(value, schema, relative)
            entries.append({"path": relative, "sha256": digest.hexdigest(), "size": size})
        required = set(output_contract.get("required_paths", [])) if isinstance(output_contract, dict) else set()
        missing = sorted(required - {entry["path"] for entry in entries})
        if missing:
            raise BoundaryViolation(f"required output paths are missing: {missing}")
        return {
            "files": sorted(entries, key=lambda item: item["path"]),
            "file_count": len(entries),
            "total_bytes": total,
        }


__all__ = ["ArtifactPackager", "LocalArtifactPackager"]
