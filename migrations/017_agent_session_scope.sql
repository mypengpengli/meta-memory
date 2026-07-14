-- A host conversation id may be reused by several local Agents.  It is a
-- transport identifier, not proof that those Agents share one transcript.
-- Keep archive/session-card evidence separated by origin Agent while Claims
-- remain shared through their workspace visibility.

ALTER TABLE sessions ADD COLUMN origin_agent_id TEXT NOT NULL DEFAULT '';

-- Preserve legacy history whenever its linked raw evidence names one and only
-- one Agent.  Mixed/unknown legacy transcripts intentionally remain in the
-- empty-agent compatibility lane rather than being guessed into an Agent's
-- private history.
UPDATE sessions
SET origin_agent_id = COALESCE((
    SELECT MIN(COALESCE(r.origin_agent_id, ''))
    FROM session_messages AS m
    JOIN raw_events AS r ON r.id = m.raw_event_id
    WHERE m.session_id = sessions.session_id
      AND COALESCE(r.origin_agent_id, '') != ''
    HAVING COUNT(DISTINCT COALESCE(r.origin_agent_id, '')) = 1
), '')
WHERE COALESCE(origin_agent_id, '') = '';

UPDATE session_cards
SET origin_agent_id = COALESCE((
    SELECT MIN(COALESCE(r.origin_agent_id, ''))
    FROM session_card_events AS sce
    JOIN raw_events AS r ON r.id = sce.raw_event_id
    WHERE sce.card_id = session_cards.id
      AND COALESCE(r.origin_agent_id, '') != ''
    HAVING COUNT(DISTINCT COALESCE(r.origin_agent_id, '')) = 1
), '')
WHERE COALESCE(origin_agent_id, '') = '';

DROP INDEX IF EXISTS idx_sessions_scope_key;
DROP INDEX IF EXISTS idx_sessions_external_scope;
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_scope_key ON sessions(scope_key);
CREATE INDEX IF NOT EXISTS idx_sessions_external_agent_scope
ON sessions(workspace_id, profile_id, subject_id, origin_agent_id, external_session_id);

-- The prior uniqueness boundary omitted origin_agent_id.  Rebuild while
-- preserving primary keys so existing raw-event and unit references remain
-- valid.
CREATE TABLE session_cards_v25 (
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
    UNIQUE(profile_id, workspace_id, subject_id, session_id, origin_agent_id)
);

INSERT INTO session_cards_v25(
    id, subject_id, subject_name, session_id, profile_id, workspace_id,
    origin_agent_id, event_start_id, event_end_id, last_event_id,
    last_extracted_event_id, source_event_ids, summary, open_questions,
    state, needs_extraction, version, created_at, updated_at
)
SELECT
    id, subject_id, subject_name, session_id, profile_id, workspace_id,
    COALESCE(origin_agent_id, ''), event_start_id, event_end_id, last_event_id,
    COALESCE(last_extracted_event_id, 0), source_event_ids, summary, open_questions,
    state, needs_extraction, version, created_at, updated_at
FROM session_cards;

DROP TABLE session_cards;
ALTER TABLE session_cards_v25 RENAME TO session_cards;
CREATE INDEX IF NOT EXISTS idx_session_cards_agent_scope
ON session_cards(profile_id, workspace_id, subject_id, origin_agent_id, session_id, last_event_id);
