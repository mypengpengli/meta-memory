-- Session processing, claim provenance, reviewable consolidation, and file versions.
CREATE TABLE IF NOT EXISTS session_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id TEXT NOT NULL,
    subject_name TEXT,
    session_id TEXT NOT NULL,
    event_start_id INTEGER,
    event_end_id INTEGER,
    last_event_id INTEGER DEFAULT 0,
    source_event_ids TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    open_questions TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'active',
    needs_extraction INTEGER DEFAULT 1,
    version INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(subject_id, session_id)
);

CREATE TABLE IF NOT EXISTS memory_units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_key TEXT UNIQUE NOT NULL,
    subject_id TEXT NOT NULL,
    subject_name TEXT,
    session_id TEXT,
    session_card_id INTEGER,
    raw_event_id INTEGER NOT NULL,
    source_event_ids TEXT NOT NULL DEFAULT '[]',
    unit_kind TEXT NOT NULL DEFAULT 'candidate',
    topic TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    confidence REAL DEFAULT 0.3,
    uncertainty REAL DEFAULT 0.7,
    importance REAL DEFAULT 0.3,
    sensitivity TEXT DEFAULT 'normal',
    source_type TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(subject_id, raw_event_id, content_hash)
);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    subject_name TEXT,
    memory_kind TEXT NOT NULL,
    topic TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT DEFAULT 'candidate',
    verification_state TEXT DEFAULT 'unverified',
    confidence REAL DEFAULT 0.3,
    importance REAL DEFAULT 0.3,
    sensitivity TEXT DEFAULT 'normal',
    valid_from TEXT,
    valid_to TEXT,
    observed_at TEXT,
    support_count INTEGER DEFAULT 1,
    memory_path TEXT,
    source_unit_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_claims_subject_hash ON claims(subject_id, content_hash);
CREATE INDEX IF NOT EXISTS idx_claims_subject_topic ON claims(subject_id, topic, status);
CREATE INDEX IF NOT EXISTS idx_claims_lifecycle ON claims(subject_id, valid_to, status);

CREATE TABLE IF NOT EXISTS claim_sources (
    claim_id TEXT NOT NULL,
    raw_event_id INTEGER NOT NULL,
    source_role TEXT DEFAULT 'supports',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(claim_id, raw_event_id, source_role)
);

CREATE TABLE IF NOT EXISTS memory_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id TEXT NOT NULL,
    from_claim_id TEXT NOT NULL,
    to_claim_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    source_event_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(from_claim_id, to_claim_id, edge_type)
);

CREATE TABLE IF NOT EXISTS consolidation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT UNIQUE NOT NULL,
    subject_id TEXT NOT NULL,
    policy TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    error TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    applied_at TEXT
);

CREATE TABLE IF NOT EXISTS memory_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_path TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT UNIQUE NOT NULL,
    subject_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_session_cards_subject ON session_cards(subject_id, session_id, last_event_id);
CREATE INDEX IF NOT EXISTS idx_memory_units_pending ON memory_units(subject_id, status, id);
CREATE INDEX IF NOT EXISTS idx_claim_sources_event ON claim_sources(raw_event_id);
CREATE INDEX IF NOT EXISTS idx_memory_edges_subject ON memory_edges(subject_id, from_claim_id, to_claim_id);

-- SQLite has no ADD COLUMN IF NOT EXISTS. db_migrations.py applies these legacy
-- columns for existing stores before the migration is marked complete.
