CREATE TABLE IF NOT EXISTS project_input_versions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    artifact_version_id TEXT NOT NULL,
    change_summary TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, version_no),
    UNIQUE(project_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS project_change_requests (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    affected_requirement_ids TEXT NOT NULL CHECK(json_valid(affected_requirement_ids)),
    requested_action TEXT NOT NULL,
    inbox_item_id TEXT,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, idempotency_key)
);
