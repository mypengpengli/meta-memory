-- Meta Memory baseline schema.  Applied transactionally by db_migrations.py.
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    title TEXT,
    subject_id TEXT,
    subject_name TEXT,
    memory_kind TEXT,
    page_role TEXT,
    canonical INTEGER DEFAULT 0,
    domain TEXT,
    topic TEXT,
    tags TEXT,
    summary TEXT,
    confidence REAL,
    importance REAL DEFAULT 0.5,
    status TEXT,
    source TEXT,
    start_at TEXT,
    end_at TEXT,
    related_people TEXT,
    related_events TEXT,
    related_topics TEXT,
    related_sources TEXT,
    supersedes TEXT,
    replaced_by TEXT,
    mtime REAL
);

CREATE TABLE IF NOT EXISTS scores (
    path TEXT PRIMARY KEY,
    hit_count INTEGER DEFAULT 0,
    confidence REAL DEFAULT 0.0,
    rank_score REAL DEFAULT 0.0,
    last_hit_at TEXT
);

CREATE TABLE IF NOT EXISTS retrieval_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    used_paths TEXT
);

CREATE TABLE IF NOT EXISTS raw_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id TEXT,
    subject_name TEXT,
    session_id TEXT,
    source_type TEXT,
    source_ref TEXT,
    content TEXT,
    content_hash TEXT,
    topic_hint TEXT,
    domain_hint TEXT,
    event_time TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    processed_state TEXT DEFAULT 'pending',
    processed_at TEXT,
    batch_id TEXT,
    classifier_kind TEXT,
    classifier_domain TEXT,
    target_memory_kind TEXT,
    target_memory_path TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS maintenance_cursor (
    subject_id TEXT PRIMARY KEY,
    last_processed_event_id INTEGER DEFAULT 0,
    last_organized_at TEXT,
    last_heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS memory_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_path TEXT,
    raw_event_id INTEGER,
    link_role TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
