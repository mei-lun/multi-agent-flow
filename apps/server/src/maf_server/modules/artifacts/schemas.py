"""Backward-compatible artifact schema validation result types."""

from __future__ import annotations

from typing import TypedDict


class ValidationIssue(TypedDict, total=False):
    field_path: str
    message: str
    schema_id: str


class ValidationResult(TypedDict):
    artifact_id: str
    content_hash: str
    schema_name: str
    schema_version: int
    valid: bool
    issues: list[ValidationIssue]


__all__ = ["ValidationIssue", "ValidationResult"]
