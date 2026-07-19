-- TASK-027: rebuildable SQLite projection of the Git coordination authority.
-- The projected_control_commit watermark advances in the same transaction as
-- task/node/event rows; Git remains the source of truth.

CREATE TABLE IF NOT EXISTS git_projector_state (
    repository_binding_id     TEXT PRIMARY KEY,
    control_branch            TEXT NOT NULL,
    projected_control_commit  TEXT,
    status                    TEXT NOT NULL,
    last_error                TEXT,
    updated_at                TEXT NOT NULL,
    CHECK (status IN ('READY', 'SYNCING', 'ERROR', 'REBUILDING'))
);

CREATE TABLE IF NOT EXISTS git_coordination_tasks (
    repository_binding_id  TEXT NOT NULL,
    task_id                TEXT NOT NULL,
    status                 TEXT NOT NULL,
    owner_node_id          TEXT,
    assignment_epoch       INTEGER,
    payload_json           TEXT NOT NULL,
    PRIMARY KEY (repository_binding_id, task_id)
);

CREATE TABLE IF NOT EXISTS git_coordination_nodes (
    repository_binding_id  TEXT NOT NULL,
    node_id                TEXT NOT NULL,
    status                 TEXT NOT NULL,
    payload_json           TEXT NOT NULL,
    PRIMARY KEY (repository_binding_id, node_id)
);

CREATE TABLE IF NOT EXISTS git_coordination_events (
    repository_binding_id  TEXT NOT NULL,
    event_id               TEXT NOT NULL,
    event_type             TEXT,
    payload_json           TEXT NOT NULL,
    PRIMARY KEY (repository_binding_id, event_id)
);
