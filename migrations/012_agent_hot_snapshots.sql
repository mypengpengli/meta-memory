-- Agent-private memories require agent-specific canonical hot snapshots.
CREATE TABLE hot_snapshots_v22 (
    snapshot_uid TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    agent_id TEXT NOT NULL DEFAULT '',
    session_id TEXT,
    content_hash TEXT NOT NULL,
    user_text TEXT NOT NULL DEFAULT '',
    agent_text TEXT NOT NULL DEFAULT '',
    current_text TEXT NOT NULL DEFAULT '',
    source_claim_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(workspace_id, profile_id, subject_id, agent_id, session_id)
);
INSERT INTO hot_snapshots_v22(snapshot_uid, workspace_id, profile_id, subject_id, session_id, content_hash, user_text, agent_text, current_text, source_claim_ids, created_at, last_checked_at)
SELECT snapshot_uid, workspace_id, profile_id, subject_id, session_id, content_hash, user_text, agent_text, current_text, source_claim_ids, created_at, last_checked_at FROM hot_snapshots;
DROP TABLE hot_snapshots;
ALTER TABLE hot_snapshots_v22 RENAME TO hot_snapshots;
CREATE INDEX IF NOT EXISTS idx_hot_snapshots_scope ON hot_snapshots(workspace_id, profile_id, subject_id, agent_id, created_at);
