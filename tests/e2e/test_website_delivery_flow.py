"""TASK-098/100 website delivery template end-to-end contract checks."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "templates" / "website_delivery"


def test_website_template_declares_seven_roles_and_workflow() -> None:
    template = yaml.safe_load((TEMPLATE / "template.yaml").read_text(encoding="utf-8"))
    workflow = yaml.safe_load((TEMPLATE / "workflow.yaml").read_text(encoding="utf-8"))
    roles = sorted(path.stem for path in (TEMPLATE / "roles").glob("*.yaml"))
    assert len(roles) == 7
    assert set(roles) >= {
        "requirements_analyst", "architect", "codebase_designer", "developer",
        "code_reviewer", "tester", "product_owner",
    }
    assert template["workflow"] == "workflow.yaml"
    assert workflow["key"] == "website_delivery_v1"
    assert workflow["nodes"][-1]["kind"] == "human_gate"


def test_delivery_stages_preserve_developer_boundary() -> None:
    workflow = yaml.safe_load((TEMPLATE / "workflow.yaml").read_text(encoding="utf-8"))
    by_id = {node["id"]: node for node in workflow["nodes"]}
    assert by_id["codebase_design"]["role"] == "codebase_designer"
    assert by_id["implementation"]["role"] == "developer"
    assert by_id["implementation"]["needs"] == ["codebase_design"]
    assert by_id["code_review"]["needs"] == ["implementation"]


def test_template_and_seed_script_do_not_contain_real_secret_material() -> None:
    for path in [*TEMPLATE.rglob("*.yaml"), ROOT / "scripts" / "seed-demo.ps1"]:
        text = path.read_text(encoding="utf-8")
        assert "sk-live" not in text
        assert "api_key:" not in text
