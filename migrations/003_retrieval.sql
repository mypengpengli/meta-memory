-- Fine-grained retrieval and explicit retrieval-use telemetry.
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_path TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    heading TEXT,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(doc_path, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc_hash ON chunks(doc_path, source_hash);
CREATE INDEX IF NOT EXISTS idx_chunks_doc_lines ON chunks(doc_path, start_line, end_line);

CREATE TABLE IF NOT EXISTS retrieval_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id TEXT NOT NULL,
    session_id TEXT,
    query_hash TEXT NOT NULL,
    query_text TEXT,
    selected_node_ids TEXT NOT NULL DEFAULT '[]',
    selected_paths TEXT NOT NULL DEFAULT '[]',
    used_node_ids TEXT NOT NULL DEFAULT '[]',
    user_confirmed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    feedback_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_retrieval_events_subject ON retrieval_events(subject_id, created_at);
