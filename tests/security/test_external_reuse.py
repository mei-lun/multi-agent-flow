"""TASK-087 security tests: metadata-only discovery and no install execution."""

from __future__ import annotations

import asyncio
from pathlib import Path

from maf_server.gateway.external_reuse.scanner import LocalSourceScanner
from maf_server.gateway.external_reuse.service import LocalExternalReuseService


class _Search:
    async def search(self, query):
        return [{"candidate_id": "c1", "url": "https://example.test/source", "content": "must-not-leak"}]


def test_external_reuse_scans_without_install_and_writes_manifest(tmp_path: Path) -> None:
    source = tmp_path / "candidate"
    source.mkdir()
    (source / "setup.py").write_text("print('never run')\n", encoding="utf-8")
    context = {"project_id": "p", "task_id": "t", "assignment_epoch": 1,
               "granted_tool_keys": ["external.search"], "workspace_path": str(tmp_path / "work")}

    async def run():
        service = LocalExternalReuseService(search_provider=_Search(), scanner=LocalSourceScanner())
        results = await service.discover(context, {})
        assert "content" not in results[0]
        evaluated = await service.evaluate(context, {"candidate_id": "c1", "source_artifact_version_id": str(source), "commit": "abc123"})
        assert evaluated["install_executed"] is False
        path = await service.record_selection(context, "c1", evaluated)
        assert path == "ExternalReuseManifest.json"

    asyncio.run(run())
