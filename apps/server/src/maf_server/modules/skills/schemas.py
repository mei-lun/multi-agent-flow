"""Skill 包、版本、扫描、测试与发布契约。"""

from typing import BinaryIO, Literal, NotRequired, TypedDict


class SkillView(TypedDict):
    id: str
    key: str
    name: str
    description: str
    latest_version: int | None
    created_at: str


class SkillVersionView(TypedDict):
    id: str
    skill_id: str
    version: int
    status: Literal["DRAFT", "TESTED", "PUBLISHED", "REJECTED"]
    content_hash: str
    entry_file: str
    declared_tools: list[str]
    declared_network_access: list[str]
    scan_report_id: str
    test_report_id: str | None
    created_at: str


class ImportSkillRequest(TypedDict):
    archive_name: str
    archive_sha256: str
    idempotency_key: str


class CreateSkillVersionRequest(TypedDict):
    upload_artifact_version_id: str
    change_summary: str
    idempotency_key: str


class TestSkillRequest(TypedDict):
    fixture_ids: list[str]
    model_profile_id: str | None
    idempotency_key: str


class SkillTestResult(TypedDict):
    report_id: str
    status: Literal["PASS", "FAIL"]
    checks: list[dict]


class PublishSkillRequest(TypedDict):
    expected_version: int
    idempotency_key: str


class SkillFileResponse(TypedDict):
    content_type: str
    content_length: int
    sha256: str
    content: bytes


class ScanResult(TypedDict):
    allowed: bool
    normalized_manifest: dict
    findings: list[dict]
    extracted_file_index: list[dict]

