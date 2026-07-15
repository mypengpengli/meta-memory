-- Runtime efficiency and observability primitives.  The FTS virtual tables
-- themselves are created conditionally by scripts/db_migrations.py so stores
-- compiled without FTS5 keep their deterministic fallback behaviour.

CREATE TABLE IF NOT EXISTS scheduler_runtime_state (
    config_key TEXT NOT NULL,
    action TEXT NOT NULL,
    desired_interval_minutes INTEGER,
    installed_interval_minutes INTEGER,
    last_started_at TEXT,
    last_completed_at TEXT,
    next_due_at TEXT,
    last_status TEXT NOT NULL DEFAULT 'never',
    last_exit_code INTEGER,
    last_error TEXT,
    last_log_path TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(config_key, action)
);

CREATE INDEX IF NOT EXISTS idx_scheduler_runtime_next_due
ON scheduler_runtime_state(config_key, action, next_due_at);

-- Operational products are intentionally separate from source evidence.  A
-- heartbeat can use this watermark to avoid a costly full cleanup on every
-- short interval while keeping its retention policy observable.
CREATE TABLE IF NOT EXISTS operational_maintenance_state (
    profile_id TEXT PRIMARY KEY,
    last_cleanup_at TEXT,
    last_compact_at TEXT,
    last_cleanup_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Records the last successful population of optional FTS indexes.  It avoids
-- COUNT(*) reconciliation during ordinary one-shot CLI launches.
CREATE TABLE IF NOT EXISTS fts_runtime_state (
    index_name TEXT PRIMARY KEY,
    source_count INTEGER NOT NULL DEFAULT 0,
    refreshed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_projection_outbox_retention
ON projection_outbox(status, completed_at);

CREATE INDEX IF NOT EXISTS idx_review_jobs_retention
ON review_jobs(status, completed_at);

CREATE INDEX IF NOT EXISTS idx_retrieval_events_retention
ON retrieval_events(created_at);

CREATE INDEX IF NOT EXISTS idx_memory_versions_retention
ON memory_versions(created_at);

CREATE INDEX IF NOT EXISTS idx_dream_runs_retention
ON dream_runs(profile_id, status, completed_at);
