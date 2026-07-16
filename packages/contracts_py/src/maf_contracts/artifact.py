"""Server–Runner 共享 Artifact manifest、哈希、血缘和上传契约。"""

from typing import Any, TypedDict


class ArtifactManifest(TypedDict):
    logical_name: str
    artifact_type: str
    schema_version: int
    content_type: str
    content_length: int
    sha256: str
    relative_source_path: str
    parent_artifact_version_ids: list[str]
    metadata: dict[str, Any]


class ArtifactUploadGrant(TypedDict):
    upload_session_id: str
    upload_url: str
    expires_at: str
    max_bytes: int
    allowed_artifact_types: list[str]


class ArtifactSubmission(TypedDict):
    attempt_id: str
    manifests: list[ArtifactManifest]
    idempotency_key: str


class ArtifactSubmissionResult(TypedDict):
    artifact_version_ids: list[str]
    validation_results: list[dict[str, Any]]

