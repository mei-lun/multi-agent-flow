"""TASK-100 final website delivery and generated status acceptance."""

from __future__ import annotations

from pathlib import Path

from maf_server.modules.git_coordination.service import LocalGitCoordinationService


ROOT = Path(__file__).resolve().parents[2]


def test_generated_status_reports_completed_remaining_and_problems() -> None:
    service = object.__new__(LocalGitCoordinationService)
    content = service.generate_status(
        tasks=[
            {"task_id": "TASK-001", "title": "done", "status": "DONE", "progress": {"problems": []}, "assignment": None},
            {"task_id": "TASK-002", "title": "remaining", "status": "IN_PROGRESS", "progress": {"problems": [{"code": "TEST_FAIL"}]}, "assignment": {"node_id": "node-a"}},
        ],
        nodes=[{"node_id": "node-a", "status": "ACTIVE", "capacity": 1}],
    )
    assert "## Completed" in content and "TASK-001" in content
    assert "## Remaining" in content and "TASK-002" in content
    assert "TEST_FAIL" in content
    assert LocalGitCoordinationService.status_digest_matches(content)


def test_website_template_contains_design_implementation_review_and_gate() -> None:
    workflow = (ROOT / "templates/website_delivery/workflow.yaml").read_text(encoding="utf-8")
    for stage in ("codebase_design", "implementation", "code_review", "testing", "acceptance", "final_review"):
        assert f"id: {stage}" in workflow
    assert "action: approve_pull_request_merge" in workflow


def test_demo_seed_script_is_deterministic_and_template_only() -> None:
    script = (ROOT / "scripts/seed-demo.ps1").read_text(encoding="utf-8")
    assert "templates/website_delivery" in script.replace("\\", "/")
    assert "api_key" not in script.lower()
