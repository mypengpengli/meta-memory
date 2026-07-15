-- Incremental Dream heartbeat coordination. Existing sessions and snapshots
-- retain their ids; generation fields only describe when a newer canonical
-- projection becomes available at the next before boundary.

ALTER TABLE sessions ADD COLUMN hot_generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE hot_snapshots ADD COLUMN generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE hot_snapshots ADD COLUMN refreshed_at TEXT;

CREATE TABLE IF NOT EXISTS dream_runtime_state (
    profile_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    heartbeat_last_started_at TEXT,
    heartbeat_last_completed_at TEXT,
    heartbeat_last_status TEXT,
    heartbeat_last_error TEXT,
    heartbeat_last_dirty_scopes INTEGER NOT NULL DEFAULT 0,
    heartbeat_last_processed_turns INTEGER NOT NULL DEFAULT 0,
    heartbeat_last_updated_claims INTEGER NOT NULL DEFAULT 0,
    heartbeat_last_updated_sessions INTEGER NOT NULL DEFAULT 0,
    heartbeat_last_new_snapshots INTEGER NOT NULL DEFAULT 0,
    deep_last_started_at TEXT,
    deep_last_completed_at TEXT,
    deep_last_status TEXT,
    deep_last_error TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(profile_id, workspace_id)
);

CREATE INDEX IF NOT EXISTS idx_dream_runtime_state_updated
ON dream_runtime_state(profile_id, updated_at DESC);

UPDATE hot_snapshots
SET refreshed_at=COALESCE(refreshed_at,last_checked_at,created_at)
WHERE refreshed_at IS NULL;
