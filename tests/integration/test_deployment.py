"""TASK-099 Compose and backup/restore deployment contract tests."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_compose_declares_healthy_server_web_and_runner() -> None:
    compose = yaml.safe_load((ROOT / "infra/compose/docker-compose.yml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {"server", "web", "runner"}
    assert compose["services"]["server"]["healthcheck"]["test"]
    assert compose["services"]["web"]["depends_on"]["server"]["condition"] == "service_healthy"
    assert compose["services"]["runner"]["depends_on"]["server"]["condition"] == "service_healthy"
    assert "maf-data" in compose["volumes"]


def test_backup_restore_scripts_cover_data_and_manifest() -> None:
    backup = (ROOT / "scripts/backup.ps1").read_text(encoding="utf-8")
    restore = (ROOT / "scripts/restore.ps1").read_text(encoding="utf-8")
    assert "backup-manifest.json" in backup
    assert "Compress-Archive" in backup
    assert "Expand-Archive" in restore
    assert "backup-manifest.json" in restore
    assert "data" in backup and "data" in restore


def test_control_projection_rebuild_is_explicitly_documented() -> None:
    readme = (ROOT / "infra/compose/README.md").read_text(encoding="utf-8")
    assert "projection" in readme
    assert "control Git" in readme
