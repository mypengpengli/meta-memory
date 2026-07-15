-- Bounded operational telemetry.  It intentionally stores identifiers,
-- counters and truncated errors only; conversation bodies and tool output stay
-- in their existing governed stores.

ALTER TABLE turns ADD COLUMN client_type TEXT NOT NULL DEFAULT 'agent';
ALTER TABLE turns ADD COLUMN client_id TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS agent_runtime_state (
    profile_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    client_type TEXT NOT NULL DEFAULT 'agent',
    client_id TEXT NOT NULL DEFAULT '',

    project_id TEXT,
    project_root TEXT,
    repository_fingerprint TEXT,

    last_before_at TEXT,
    last_after_at TEXT,
    last_write_at TEXT,
    last_retrieval_at TEXT,
    last_turn_uid TEXT,
    last_session_id TEXT,
    last_retrieval_count INTEGER NOT NULL DEFAULT 0,
    last_retrieval_duration_ms INTEGER NOT NULL DEFAULT 0,
    total_before INTEGER NOT NULL DEFAULT 0,
    total_after INTEGER NOT NULL DEFAULT 0,
    total_degraded INTEGER NOT NULL DEFAULT 0,

    last_error_at TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY(profile_id, agent_id, workspace_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_runtime_project_root
ON agent_runtime_state(profile_id, project_root, project_id);

CREATE INDEX IF NOT EXISTS idx_agent_runtime_fingerprint
ON agent_runtime_state(profile_id, repository_fingerprint, project_id);

CREATE TABLE IF NOT EXISTS runtime_error_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    workspace_id TEXT,
    turn_uid TEXT,
    phase TEXT NOT NULL,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_runtime_error_log_retention
ON runtime_error_log(profile_id, created_at);
