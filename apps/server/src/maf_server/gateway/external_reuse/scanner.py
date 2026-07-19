"""复用前轻量源码和依赖安全扫描接口。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Protocol


class ExternalSourceScanner(Protocol):
    async def scan(self, source_artifact_version_id: str, ecosystem: str | None) -> dict:
        """检查可疑脚本、二进制、依赖漏洞和密钥痕迹；不执行候选项目安装脚本。"""
        ...


class LocalSourceScanner:
    """Static scanner that never executes candidate code or package scripts."""

    _SCRIPT_NAMES = {"setup.py", "install.sh", "install.bash", "preinstall", "postinstall", "Makefile"}
    _SECRET_PATTERNS = (
        re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}"),
        re.compile(r"-----BEGIN (?:RSA|OPENSSH|EC) PRIVATE KEY-----"),
    )

    async def scan(self, source_artifact_version_id: str, ecosystem: str | None = None) -> dict:
        root = Path(source_artifact_version_id).expanduser()
        findings: list[dict] = []
        files: list[str] = []
        digest = hashlib.sha256()
        if root.is_dir():
            for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
                rel = path.relative_to(root).as_posix()
                files.append(rel)
                data = path.read_bytes()[:2 * 1024 * 1024]
                digest.update(rel.encode() + b"\0" + data)
                if path.name in self._SCRIPT_NAMES or path.suffix in {".sh", ".bat", ".ps1"}:
                    findings.append({"severity": "WARNING", "code": "executable_script", "path": rel})
                text = data.decode("utf-8", errors="ignore")
                for pattern in self._SECRET_PATTERNS:
                    if pattern.search(text):
                        findings.append({"severity": "BLOCK", "code": "secret_pattern", "path": rel})
                        break
        elif root.is_file():
            data = root.read_bytes()
            digest.update(data)
            files.append(root.name)
        else:
            findings.append({"severity": "BLOCK", "code": "source_not_found"})
        blocked = any(item["severity"] == "BLOCK" for item in findings)
        return {"safe": not blocked, "findings": findings, "files": files,
                "content_hash": digest.hexdigest(), "ecosystem": ecosystem}


__all__ = ["ExternalSourceScanner", "LocalSourceScanner"]
