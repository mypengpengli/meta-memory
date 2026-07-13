CREATE TABLE IF NOT EXISTS entities (
    entity_uid TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(workspace_id, canonical_name, entity_type)
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_uid TEXT NOT NULL,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    source_event_id INTEGER,
    confidence REAL DEFAULT 0.5,
    UNIQUE(entity_uid, normalized_alias)
);

CREATE INDEX IF NOT EXISTS idx_entity_aliases_normalized ON entity_aliases(normalized_alias);

CREATE TABLE IF NOT EXISTS claim_entities (
    claim_uid TEXT NOT NULL,
    entity_uid TEXT NOT NULL,
    role TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    PRIMARY KEY(claim_uid, entity_uid, role)
);

CREATE INDEX IF NOT EXISTS idx_claim_entities_entity ON claim_entities(entity_uid, role);

CREATE TABLE IF NOT EXISTS memory_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_uid TEXT NOT NULL,
    retrieval_uid TEXT,
    feedback_type TEXT NOT NULL,
    source TEXT NOT NULL,
    weight REAL NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_memory_feedback_claim ON memory_feedback(claim_uid, created_at);

CREATE TABLE IF NOT EXISTS procedural_learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    learning_uid TEXT NOT NULL UNIQUE,
    subject_id TEXT NOT NULL,
    domain TEXT,
    task_class TEXT NOT NULL,
    trigger_text TEXT,
    instruction_text TEXT NOT NULL,
    pitfall_text TEXT,
    source_event_ids TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    status TEXT DEFAULT 'candidate',
    target_skill TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_procedural_learnings_subject ON procedural_learnings(subject_id, task_class, status);
