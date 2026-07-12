-- Optional embeddings. The deterministic retrieval path remains fully usable
-- when this table is empty or no embedding provider is configured.
CREATE TABLE IF NOT EXISTS embeddings (
    node_type TEXT NOT NULL,
    node_id TEXT NOT NULL,
    subject_id TEXT,
    model TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(node_type, node_id, model)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_subject ON embeddings(subject_id, node_type, model);
