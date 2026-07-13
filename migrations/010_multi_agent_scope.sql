-- 2.2: make the complete evidence and projection chain safe for shared agents.
ALTER TABLE raw_events ADD COLUMN profile_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE raw_events ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'global';
ALTER TABLE raw_events ADD COLUMN origin_agent_id TEXT NOT NULL DEFAULT '';
ALTER TABLE raw_events ADD COLUMN event_uid TEXT;
ALTER TABLE raw_events ADD COLUMN idempotency_key TEXT;
ALTER TABLE raw_events ADD COLUMN visibility_scope TEXT NOT NULL DEFAULT 'workspace';
CREATE INDEX IF NOT EXISTS idx_raw_events_scope ON raw_events(profile_id, workspace_id, subject_id, session_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_event_idempotency ON raw_events(profile_id, workspace_id, origin_agent_id, idempotency_key) WHERE idempotency_key IS NOT NULL AND idempotency_key!='';

-- session_cards used to have UNIQUE(subject_id, session_id), which is not
-- sufficient when the same external session label exists in two workspaces.
CREATE TABLE session_cards_v22 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id TEXT NOT NULL,
    subject_name TEXT,
    session_id TEXT NOT NULL,
    profile_id TEXT NOT NULL DEFAULT 'default',
    workspace_id TEXT NOT NULL DEFAULT 'global',
    origin_agent_id TEXT NOT NULL DEFAULT '',
    event_start_id INTEGER,
    event_end_id INTEGER,
    last_event_id INTEGER DEFAULT 0,
    last_extracted_event_id INTEGER NOT NULL DEFAULT 0,
    source_event_ids TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    open_questions TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'active',
    needs_extraction INTEGER DEFAULT 1,
    version INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(profile_id, workspace_id, subject_id, session_id)
);
INSERT INTO session_cards_v22(id, subject_id, subject_name, session_id, event_start_id, event_end_id, last_event_id, last_extracted_event_id, source_event_ids, summary, open_questions, state, needs_extraction, version, created_at, updated_at)
SELECT id, subject_id, subject_name, session_id, event_start_id, event_end_id, last_event_id, COALESCE(last_extracted_event_id, 0), source_event_ids, summary, open_questions, state, needs_extraction, version, created_at, updated_at FROM session_cards;
DROP TABLE session_cards;
ALTER TABLE session_cards_v22 RENAME TO session_cards;
CREATE INDEX IF NOT EXISTS idx_session_cards_scope ON session_cards(profile_id, workspace_id, subject_id, session_id, last_event_id);

ALTER TABLE memory_units ADD COLUMN profile_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE memory_units ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'global';
ALTER TABLE memory_units ADD COLUMN visibility_scope TEXT NOT NULL DEFAULT 'workspace';
ALTER TABLE memory_units ADD COLUMN origin_agent_id TEXT NOT NULL DEFAULT '';
ALTER TABLE memory_units ADD COLUMN owner_agent_id TEXT;
CREATE INDEX IF NOT EXISTS idx_memory_units_scope ON memory_units(profile_id, workspace_id, subject_id, status, id);

ALTER TABLE claims ADD COLUMN profile_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE claims ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'global';
ALTER TABLE claims ADD COLUMN visibility_scope TEXT NOT NULL DEFAULT 'workspace';
ALTER TABLE claims ADD COLUMN owner_agent_id TEXT;
ALTER TABLE claims ADD COLUMN origin_agent_id TEXT NOT NULL DEFAULT '';
ALTER TABLE claims ADD COLUMN semantic_key TEXT;
CREATE INDEX IF NOT EXISTS idx_claims_scope ON claims(profile_id, workspace_id, subject_id, status, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_claim_semantic_key ON claims(semantic_key) WHERE status='active' AND semantic_key IS NOT NULL AND semantic_key!='';

ALTER TABLE documents ADD COLUMN profile_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE documents ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'global';
ALTER TABLE documents ADD COLUMN visibility_scope TEXT NOT NULL DEFAULT 'workspace';
ALTER TABLE documents ADD COLUMN owner_agent_id TEXT;
CREATE INDEX IF NOT EXISTS idx_documents_scope ON documents(profile_id, workspace_id, subject_id, status);

ALTER TABLE review_jobs ADD COLUMN profile_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE review_jobs ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'global';
ALTER TABLE review_jobs ADD COLUMN origin_agent_id TEXT NOT NULL DEFAULT '';

ALTER TABLE write_proposals ADD COLUMN profile_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE write_proposals ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'global';
ALTER TABLE write_proposals ADD COLUMN origin_agent_id TEXT NOT NULL DEFAULT '';
ALTER TABLE write_proposals ADD COLUMN reviewed_by_agent_id TEXT;

ALTER TABLE procedural_learnings ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'global';
ALTER TABLE procedural_learnings ADD COLUMN visibility_scope TEXT NOT NULL DEFAULT 'workspace';
ALTER TABLE procedural_learnings ADD COLUMN owner_agent_id TEXT;
ALTER TABLE procedural_learnings ADD COLUMN security_state TEXT NOT NULL DEFAULT 'clean';
ALTER TABLE procedural_learnings ADD COLUMN prompt_eligible INTEGER NOT NULL DEFAULT 0;

ALTER TABLE projection_outbox ADD COLUMN lease_owner TEXT;
ALTER TABLE projection_outbox ADD COLUMN leased_until TEXT;
ALTER TABLE projection_outbox ADD COLUMN next_retry_at TEXT;
ALTER TABLE projection_outbox ADD COLUMN dead_letter_at TEXT;
CREATE INDEX IF NOT EXISTS idx_projection_outbox_lease ON projection_outbox(status, next_retry_at, leased_until, created_at);
