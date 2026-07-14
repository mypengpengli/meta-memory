-- A turn records the durable boundary around one user request and one
-- assistant response.  User evidence must survive even when the host Agent
-- never reaches its post-answer hook.
CREATE TABLE IF NOT EXISTS turns (
    turn_uid TEXT PRIMARY KEY,

    profile_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    origin_agent_id TEXT NOT NULL,

    external_session_id TEXT NOT NULL,
    internal_session_id TEXT,

    user_event_id INTEGER,
    assistant_event_id INTEGER,
    review_job_uid TEXT,

    request_hash TEXT,
    response_hash TEXT,

    status TEXT NOT NULL DEFAULT 'started',
    context_status TEXT NOT NULL DEFAULT 'pending',

    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_turns_session
ON turns(profile_id, workspace_id, subject_id, origin_agent_id, external_session_id, started_at);

CREATE INDEX IF NOT EXISTS idx_turns_status
ON turns(status, started_at);

ALTER TABLE raw_events ADD COLUMN turn_uid TEXT;
ALTER TABLE raw_events ADD COLUMN message_role TEXT;
ALTER TABLE raw_events ADD COLUMN message_sequence INTEGER;

CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_event_turn_role
ON raw_events(profile_id, workspace_id, origin_agent_id, turn_uid, message_role)
WHERE turn_uid IS NOT NULL
  AND turn_uid != ''
  AND message_role IN ('user', 'assistant');
