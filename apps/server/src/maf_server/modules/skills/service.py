"""Skill registration, secure package scanning, evaluation and publishing."""

from __future__ import annotations

import hashlib
import io
import json
import posixpath
import stat
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Awaitable, BinaryIO, Callable, Protocol
from maf_contracts.common import ActorContext
from .repository import InMemorySkillRepository, SkillRepository
from .schemas import *


class SkillPackageScanner(Protocol):
    def scan(self, archive: BinaryIO) -> ScanResult:
        """在写入正式 SkillStore 前扫描压缩包。

        必须限制压缩后/解压后大小、文件数、路径长度和压缩比；拒绝绝对路径、`..`、链接、
        设备文件和重复规范化路径；解析 manifest，列出脚本、外网与 Tool 声明。输出只给出
        可复现结果，不执行包内代码。
        """
        ...


class SkillService(Protocol):
    async def import_package(
        self, actor: ActorContext, request: ImportSkillRequest, archive: BinaryIO
    ) -> SkillVersionView:
        """导入新 Skill 或新包的首版本。

        先流式计算 SHA-256 并核对请求，再调用 Scanner；扫描通过后按内容寻址保存原包与
        规范化文件，创建 DRAFT 版本。失败包进入隔离区且不可被 Runtime 读取。
        """
        ...

    async def create_version(
        self, actor: ActorContext, skill_id: str, request: CreateSkillVersionRequest
    ) -> SkillVersionView:
        """从已上传 Artifact 创建递增的不可变 DRAFT 版本，并重新扫描全部内容。"""
        ...

    async def test_version(
        self, actor: ActorContext, version_id: str, request: TestSkillRequest
    ) -> SkillTestResult:
        """在隔离 Runner 中执行固定夹具测试。"""
        ...

    async def publish_version(
        self, actor: ActorContext, version_id: str, request: PublishSkillRequest
    ) -> SkillVersionView:
        """发布已扫描且测试通过的不可变版本。"""
        ...


class SecureSkillPackageScanner:
    """Inspect a ZIP package without importing or executing its contents."""

    def __init__(
        self,
        *,
        max_archive_bytes: int = 16 * 1024 * 1024,
        max_extracted_bytes: int = 64 * 1024 * 1024,
        max_files: int = 512,
        max_ratio: int = 100,
        max_path_length: int = 240,
    ) -> None:
        self.max_archive_bytes = max_archive_bytes
        self.max_extracted_bytes = max_extracted_bytes
        self.max_files = max_files
        self.max_ratio = max_ratio
        self.max_path_length = max_path_length

    def scan(self, archive: BinaryIO) -> ScanResult:
        findings: list[dict] = []
        index: list[dict] = []
        manifest: dict[str, Any] = {}
        try:
            payload = _read_limited(archive, self.max_archive_bytes)
            with zipfile.ZipFile(io.BytesIO(payload)) as package:
                infos = package.infolist()
                if len(infos) > self.max_files:
                    raise ValueError("archive contains too many files")
                total = 0
                seen: set[str] = set()
                for info in infos:
                    path = _safe_member_path(info.filename, self.max_path_length)
                    mode = info.external_attr >> 16
                    if stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode):
                        raise ValueError(f"unsupported special file: {path}")
                    if info.is_dir():
                        continue
                    normalized = path.casefold()
                    if normalized in seen:
                        raise ValueError(f"duplicate normalized path: {path}")
                    seen.add(normalized)
                    total += info.file_size
                    if total > self.max_extracted_bytes:
                        raise ValueError("archive extracted size limit exceeded")
                    compressed = max(info.compress_size, 1)
                    if info.file_size / compressed > self.max_ratio:
                        raise ValueError(f"archive compression ratio exceeded: {path}")
                    digest = hashlib.sha256()
                    content = bytearray()
                    with package.open(info, "r") as source:
                        while True:
                            chunk = source.read(64 * 1024)
                            if not chunk:
                                break
                            digest.update(chunk)
                            if path in {"skill.json", "manifest.json", "skill.yaml", "skill.yml"}:
                                content.extend(chunk)
                    index.append({"path": path, "sha256": digest.hexdigest(), "size": info.file_size})
                    if content:
                        manifest = _parse_manifest(bytes(content), path)
                if not manifest:
                    raise ValueError("skill manifest is missing")
                _validate_manifest(manifest, {item["path"] for item in index})
        except (ValueError, zipfile.BadZipFile, OSError) as exc:
            findings.append({"severity": "BLOCK", "code": "PACKAGE_REJECTED", "message": str(exc)})
            return ScanResult(
                allowed=False,
                normalized_manifest={},
                findings=findings,
                extracted_file_index=[],
            )
        return ScanResult(
            allowed=True,
            normalized_manifest=manifest,
            findings=findings,
            extracted_file_index=sorted(index, key=lambda item: item["path"]),
        )


def _read_limited(stream: BinaryIO, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise ValueError("archive size limit exceeded")
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_member_path(name: str, max_length: int) -> str:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("invalid archive path")
    if len(name) > max_length or name.startswith("/"):
        raise ValueError("archive path is absolute or too long")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("archive path traversal detected")
    normalized = posixpath.normpath(name)
    if normalized.startswith("../") or normalized == "..":
        raise ValueError("archive path traversal detected")
    return normalized.rstrip("/")


def _parse_manifest(content: bytes, name: str) -> dict[str, Any]:
    try:
        if name.endswith(".json"):
            value = json.loads(content.decode("utf-8"))
        else:
            import yaml
            value = yaml.safe_load(content.decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid skill manifest") from exc
    if not isinstance(value, dict):
        raise ValueError("skill manifest must be an object")
    return value


def _validate_manifest(manifest: dict[str, Any], files: set[str]) -> None:
    required = ("key", "name", "entry_file")
    if any(not isinstance(manifest.get(key), str) or not manifest[key] for key in required):
        raise ValueError("skill manifest is missing required fields")
    entry = _safe_member_path(manifest["entry_file"], 240)
    if entry not in files:
        raise ValueError("skill entry_file does not exist")
    for field in ("tools", "network_access"):
        value = manifest.get(field, [])
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ValueError(f"skill manifest {field} must be a string array")


SkillEvaluator = Callable[[SkillVersionView, TestSkillRequest, dict[str, Any]], Awaitable[dict[str, Any]]]


class SkillServiceImpl:
    """In-process application service with immutable published versions."""

    def __init__(
        self,
        repository: SkillRepository | None = None,
        *,
        scanner: SecureSkillPackageScanner | None = None,
        evaluator: SkillEvaluator | None = None,
        artifact_loader: Callable[[str], BinaryIO] | None = None,
    ) -> None:
        self.repository = repository or InMemorySkillRepository()
        self.scanner = scanner or SecureSkillPackageScanner()
        self.evaluator = evaluator
        self.artifact_loader = artifact_loader
        self._packages: dict[str, bytes] = {}
        self._manifests: dict[str, dict[str, Any]] = {}
        self._reports: dict[str, SkillTestResult] = {}

    async def import_package(self, actor: ActorContext, request: ImportSkillRequest, archive: BinaryIO) -> SkillVersionView:
        payload = _read_limited(archive, self.scanner.max_archive_bytes)
        digest = hashlib.sha256(payload).hexdigest()
        if digest != request["archive_sha256"]:
            raise ValueError("archive sha256 mismatch")
        result = self.scanner.scan(io.BytesIO(payload))
        if not result["allowed"]:
            raise ValueError("skill package rejected")
        manifest = result["normalized_manifest"]
        skill_id = f"skill-{manifest['key']}"
        repo = self.repository
        existing = await repo.get_skill(skill_id)
        version_no = (existing["latest_version"] or 0) + 1 if existing else 1
        version_id = f"{skill_id}:v{version_no}"
        now = datetime.now(timezone.utc).isoformat()
        version = SkillVersionView(
            id=version_id,
            skill_id=skill_id,
            version=version_no,
            status="DRAFT",
            content_hash=digest,
            entry_file=manifest["entry_file"],
            declared_tools=list(manifest.get("tools", [])),
            declared_network_access=list(manifest.get("network_access", [])),
            scan_report_id=f"scan-{uuid.uuid4()}",
            test_report_id=None,
            created_at=now,
        )
        if isinstance(repo, InMemorySkillRepository):
            repo.skills[skill_id] = SkillView(
                id=skill_id,
                key=manifest["key"],
                name=manifest["name"],
                description=str(manifest.get("description", "")),
                latest_version=version_no,
                created_at=now,
            )
            with zipfile.ZipFile(io.BytesIO(payload)) as package:
                for item in result["extracted_file_index"]:
                    content = package.read(item["path"])
                    repo.files[(version_id, item["path"])] = {**item, "content": content}
        await repo.save_version(version)
        self._packages.setdefault(digest, payload)
        self._manifests[version_id] = dict(manifest)
        return version

    async def create_version(self, actor: ActorContext, skill_id: str, request: CreateSkillVersionRequest) -> SkillVersionView:
        if self.artifact_loader is None:
            raise ValueError("artifact loader is not configured")
        archive = self.artifact_loader(request["upload_artifact_version_id"])
        payload = _read_limited(archive, self.scanner.max_archive_bytes)
        return await self.import_package(actor, ImportSkillRequest(
            archive_name=request["upload_artifact_version_id"],
            archive_sha256=hashlib.sha256(payload).hexdigest(),
            idempotency_key=request["idempotency_key"],
        ), io.BytesIO(payload))

    async def test_version(self, actor: ActorContext, version_id: str, request: TestSkillRequest) -> SkillTestResult:
        version = await self.repository.get_version(version_id)
        if version is None or version["status"] == "PUBLISHED":
            raise ValueError("skill version is not testable")
        if self.evaluator is None:
            raise ValueError("isolated skill evaluator is not configured")
        permissions = {
            "tools": list(version["declared_tools"]),
            "network_access": list(version["declared_network_access"]),
            "read_only_root": True,
        }
        first = await self.evaluator(version, request, permissions)
        second = await self.evaluator(version, request, permissions)
        stable = json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
        passed = stable and bool(first.get("passed"))
        report = SkillTestResult(
            report_id=f"skill-report-{uuid.uuid4()}",
            status="PASS" if passed else "FAIL",
            checks=[{"name": "deterministic", "passed": stable}, *list(first.get("checks", []))],
        )
        version["status"] = "TESTED" if passed else "REJECTED"
        version["test_report_id"] = report["report_id"]
        await self.repository.save_version(version)
        self._reports[report["report_id"]] = report
        return report

    async def publish_version(self, actor: ActorContext, version_id: str, request: PublishSkillRequest) -> SkillVersionView:
        version = await self.repository.get_version(version_id)
        if version is None:
            raise KeyError(version_id)
        if version["version"] != request["expected_version"]:
            raise ValueError("skill version conflict")
        if version["status"] != "TESTED" or not version["test_report_id"]:
            raise ValueError("only tested skill versions may be published")
        version["status"] = "PUBLISHED"
        return await self.repository.save_version(version)

    async def export_git_index(self, version_id: str) -> dict[str, Any]:
        version = await self.repository.get_version(version_id)
        if version is None or version["status"] != "PUBLISHED":
            raise ValueError("only published skill versions may be distributed")
        if not isinstance(self.repository, InMemorySkillRepository):
            raise ValueError("repository does not expose a file index")
        files = [
            {"path": path, "sha256": item["sha256"], "size": item["size"]}
            for (bound_version, path), item in self.repository.files.items()
            if bound_version == version_id
        ]
        return {"version_id": version_id, "content_hash": version["content_hash"], "files": sorted(files, key=lambda item: item["path"])}


__all__ = [
    "SecureSkillPackageScanner",
    "SkillPackageScanner",
    "SkillService",
    "SkillServiceImpl",
]
