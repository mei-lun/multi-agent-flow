from __future__ import annotations

from unittest.mock import Mock

from maf_server.modules.git_coordination.service import LocalGitCoordinationService


def _service(logger=None) -> LocalGitCoordinationService:
    return LocalGitCoordinationService(
        git_cli=Mock(),
        repository_path="/tmp/not-used",
        logger=logger,
    )


def _tasks():
    return [
        {
            "task_id": "TASK-002",
            "title": "Remain",
            "status": "IN_PROGRESS",
            "assignment": {"node_id": "node-b", "assignment_epoch": 1},
            "progress": {"problems": [{"code": "WAITING", "detail": "input"}]},
        },
        {
            "task_id": "TASK-001",
            "title": "Done",
            "status": "DONE",
            "assignment": None,
            "progress": {"problems": []},
        },
    ]


def _nodes():
    return [
        {"node_id": "node-b", "status": "ACTIVE", "capacity": 2},
        {"node_id": "node-a", "status": "OFFLINE", "capacity": 1},
    ]


def test_equal_authority_generates_byte_identical_output() -> None:
    service = _service()
    first = service.generate_status(tasks=_tasks(), nodes=_nodes())
    second = service.generate_status(
        tasks=list(reversed(_tasks())), nodes=list(reversed(_nodes()))
    )
    assert first == second
    assert LocalGitCoordinationService.status_digest_matches(first)


def test_header_declares_generated_file_and_protocol() -> None:
    content = _service().generate_status(tasks=_tasks(), nodes=_nodes())
    assert "GENERATED FILE. Do not edit manually." in content
    assert "Protocol: `maf/status-v1`" in content
    assert "## Completed" in content
    assert "## Remaining" in content
    assert "## Problems" in content
    assert "## Node Utilization" in content


def test_manual_change_is_detected_warned_and_overwritten() -> None:
    logger = Mock()
    service = _service(logger=logger)
    generated = service.generate_status(tasks=_tasks(), nodes=_nodes())
    modified = generated.replace("Remain", "MANUAL CHANGE")
    assert not service.status_digest_matches(modified)

    regenerated = service.generate_status(
        tasks=_tasks(), nodes=_nodes(), current_status=modified
    )
    assert regenerated == generated
    logger.warning.assert_called_once_with(
        "generated_status_modified", reason="digest_missing_or_mismatched"
    )
