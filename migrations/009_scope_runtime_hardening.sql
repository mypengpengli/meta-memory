-- 2.2 correctness boundary: every runtime identity is scoped by workspace,
-- profile, and subject.  Existing rows retain their legacy session id as the
-- external id; new rows use an opaque UUID primary key.
ALTER TABLE sessions ADD COLUMN external_session_id TEXT;
ALTER TABLE sessions ADD COLUMN scope_key TEXT;
ALTER TABLE sessions ADD COLUMN hot_snapshot_uid TEXT;
ALTER TABLE sessions ADD COLUMN hot_snapshot_hash TEXT;
ALTER TABLE sessions ADD COLUMN hot_snapshot_created_at TEXT;

UPDATE sessions
SET external_session_id = COALESCE(NULLIF(external_session_id, ''), session_id)
WHERE external_session_id IS NULL OR external_session_id = '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_scope_key ON sessions(scope_key);
CREATE INDEX IF NOT EXISTS idx_sessions_external_scope ON sessions(workspace_id, profile_id, subject_id, external_session_id);

CREATE TABLE IF NOT EXISTS hot_snapshots (
    snapshot_uid TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    session_id TEXT,
    content_hash TEXT NOT NULL,
    user_text TEXT NOT NULL DEFAULT '',
    agent_text TEXT NOT NULL DEFAULT '',
    current_text TEXT NOT NULL DEFAULT '',
    source_claim_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(workspace_id, profile_id, subject_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_hot_snapshots_scope ON hot_snapshots(workspace_id, profile_id, subject_id, created_at);

ALTER TABLE session_cards ADD COLUMN last_extracted_event_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE maintenance_cursor ADD COLUMN last_check_at TEXT;

ALTER TABLE review_jobs ADD COLUMN lease_owner TEXT;
ALTER TABLE review_jobs ADD COLUMN leased_until TEXT;
CREATE INDEX IF NOT EXISTS idx_review_jobs_lease ON review_jobs(status, leased_until, next_retry_at);

CREATE TABLE IF NOT EXISTS projection_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload_hash TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    UNIQUE(entity_type, entity_id, operation, payload_hash)
);
CREATE INDEX IF NOT EXISTS idx_projection_outbox_pending ON projection_outbox(status, created_at);

ALTER TABLE entity_aliases ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'default';
CREATE INDEX IF NOT EXISTS idx_entity_alias_scope ON entity_aliases(workspace_id, normalized_alias);

ALTER TABLE memory_feedback ADD COLUMN raw_event_id INTEGER;

ALTER TABLE memory_units ADD COLUMN clause_index INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_units ADD COLUMN extraction_version TEXT NOT NULL DEFAULT 'rules-v2';
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_unit_source_clause ON memory_units(subject_id, raw_event_id, clause_index, extraction_version);
