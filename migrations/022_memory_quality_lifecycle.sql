-- Memory-quality and continuity projections.  These columns/tables are
-- derivatives and lifecycle metadata; raw events and Claims remain the
-- authoritative evidence.

ALTER TABLE memory_units ADD COLUMN retention_class TEXT NOT NULL DEFAULT 'long_term_candidate';
ALTER TABLE memory_units ADD COLUMN retention_reason TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_memory_units_retention
ON memory_units(profile_id, workspace_id, subject_id, retention_class, status, id);

ALTER TABLE session_cards ADD COLUMN rolling_state_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE session_cards ADD COLUMN rolling_state_version INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_session_cards_continuity
ON session_cards(profile_id, workspace_id, subject_id, summary_visibility, last_completed_turn_at DESC);

-- A started turn can be deliberately renewed by a long-running Agent.  The
-- recovery worker must use this activity boundary rather than its creation
-- time, otherwise normal coding/research turns are abandoned prematurely.
ALTER TABLE turns ADD COLUMN last_active_at TEXT;
ALTER TABLE turns ADD COLUMN reopened_at TEXT;
ALTER TABLE turns ADD COLUMN reopen_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE turns ADD COLUMN completion_kind TEXT NOT NULL DEFAULT 'normal';
UPDATE turns
SET last_active_at=COALESCE(NULLIF(last_active_at, ''), updated_at, started_at)
WHERE last_active_at IS NULL OR last_active_at='';
CREATE INDEX IF NOT EXISTS idx_turns_activity_recovery
ON turns(profile_id, status, last_active_at);

ALTER TABLE dream_nodes ADD COLUMN last_run_uid TEXT;
CREATE INDEX IF NOT EXISTS idx_dream_nodes_last_run
ON dream_nodes(profile_id, last_run_uid, updated_at);

-- Dream runs may produce one report per workspace.  The source hash is kept
-- separately per scope so an unchanged scope becomes a true no-op instead of
-- creating empty or duplicate reports.
CREATE TABLE IF NOT EXISTS dream_scope_state (
    profile_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    source_hash TEXT NOT NULL DEFAULT '',
    last_run_uid TEXT,
    last_completed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(profile_id, workspace_id, subject_id)
);

CREATE TABLE IF NOT EXISTS dream_run_reports (
    run_uid TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    source_hash TEXT NOT NULL DEFAULT '',
    report_path TEXT NOT NULL,
    node_count INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(run_uid, workspace_id, subject_id)
);
CREATE INDEX IF NOT EXISTS idx_dream_run_reports_listing
ON dream_run_reports(profile_id, archived_at, created_at DESC);
