-- 2.2 operational hardening.  Kept separate from 010 because some early
-- adopters have already applied that migration.
CREATE TABLE IF NOT EXISTS session_card_events (
    card_id INTEGER NOT NULL,
    raw_event_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(card_id, raw_event_id)
);
INSERT OR IGNORE INTO session_card_events(card_id, raw_event_id)
SELECT session_card_id, id FROM raw_events WHERE session_card_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_session_card_events_raw ON session_card_events(raw_event_id, card_id);

ALTER TABLE chunks ADD COLUMN profile_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE chunks ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'global';
ALTER TABLE chunks ADD COLUMN visibility_scope TEXT NOT NULL DEFAULT 'workspace';
ALTER TABLE chunks ADD COLUMN owner_agent_id TEXT;
CREATE INDEX IF NOT EXISTS idx_chunks_scope ON chunks(profile_id, workspace_id, doc_path);

ALTER TABLE procedural_learnings ADD COLUMN profile_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE claims ADD COLUMN needs_reextract INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_review_jobs_scope_pending
ON review_jobs(profile_id, workspace_id, subject_id, status, next_retry_at, created_at);
CREATE INDEX IF NOT EXISTS idx_procedures_scope
ON procedural_learnings(subject_id, workspace_id, status, prompt_eligible);
