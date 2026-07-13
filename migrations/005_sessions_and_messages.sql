-- Durable session archive.  FTS is created opportunistically by db_migrations.py
-- so installations whose SQLite omits FTS5 continue to work.
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    parent_session_id TEXT,
    subject_id TEXT NOT NULL,
    profile_id TEXT NOT NULL DEFAULT 'default',
    workspace_id TEXT NOT NULL DEFAULT 'default',
    source TEXT NOT NULL DEFAULT 'interactive',
    title TEXT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_active_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS session_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    raw_event_id INTEGER,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    tool_name TEXT,
    tool_call_id TEXT,
    tool_calls_json TEXT,
    timestamp TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    content_hash TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
    UNIQUE(session_id, content_hash, role, raw_event_id)
);

CREATE INDEX IF NOT EXISTS idx_session_messages_session ON session_messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_sessions_subject_active ON sessions(subject_id, last_active_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_lineage ON sessions(subject_id, parent_session_id, started_at);
