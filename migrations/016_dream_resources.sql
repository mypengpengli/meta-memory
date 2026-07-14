-- Structured deferred synthesis and imported-resource evidence.  Neither
-- table changes the truth source (claims); both remain auditable derivatives.

CREATE TABLE IF NOT EXISTS dream_runs (
    run_uid TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    workspace_id TEXT,
    subject_id TEXT,
    agent_id TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    provider TEXT NOT NULL DEFAULT 'deterministic',
    scan_days INTEGER NOT NULL DEFAULT 7,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS dream_nodes (
    dream_uid TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    visibility_scope TEXT NOT NULL DEFAULT 'workspace',
    owner_agent_id TEXT,
    node_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source_claim_ids TEXT NOT NULL DEFAULT '[]',
    source_hash TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    inference_level TEXT NOT NULL DEFAULT 'extractive',
    status TEXT NOT NULL DEFAULT 'inferred',
    prompt_eligible INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_dream_node_source
ON dream_nodes(profile_id, workspace_id, subject_id, node_type, source_hash);

CREATE INDEX IF NOT EXISTS idx_dream_node_retrieval
ON dream_nodes(profile_id, workspace_id, subject_id, status, prompt_eligible, updated_at);

CREATE TABLE IF NOT EXISTS resource_imports (
    resource_uid TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    byte_size INTEGER NOT NULL DEFAULT 0,
    modified_at REAL,
    encoding TEXT NOT NULL DEFAULT 'utf-8',
    raw_event_id INTEGER,
    card_path TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(profile_id, workspace_id, subject_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_resource_import_scope
ON resource_imports(profile_id, workspace_id, subject_id, updated_at);

CREATE TABLE IF NOT EXISTS resource_chunks (
    chunk_uid TEXT PRIMARY KEY,
    resource_uid TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    start_offset INTEGER NOT NULL DEFAULT 0,
    end_offset INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(resource_uid) REFERENCES resource_imports(resource_uid) ON DELETE CASCADE,
    UNIQUE(resource_uid, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_resource_chunks_resource
ON resource_chunks(resource_uid, chunk_index);
