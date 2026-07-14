-- Runtime coordination for the public local scheduler.  This migration is
-- append-only: stores that already applied 001-014 keep their checksums.

CREATE TABLE IF NOT EXISTS runtime_locks (
    lock_name TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    leased_until TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_runtime_locks_lease
ON runtime_locks(leased_until);

-- Derived runtime products (hot snapshots and Dream reports) are keyed at
-- least as narrowly as the current data plane.  The empty agent_id denotes
-- the normal shared snapshot; non-empty values allow a later agent-private
-- producer to use the same state machine without widening visibility.
CREATE TABLE IF NOT EXISTS workspace_runtime_state (
    profile_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    agent_id TEXT NOT NULL DEFAULT '',

    hot_dirty INTEGER NOT NULL DEFAULT 1,
    dream_dirty INTEGER NOT NULL DEFAULT 1,

    claim_generation INTEGER NOT NULL DEFAULT 0,
    hot_generation INTEGER NOT NULL DEFAULT 0,
    dream_generation INTEGER NOT NULL DEFAULT 0,

    last_maintained_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY(profile_id, workspace_id, subject_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_runtime_state_hot_dirty
ON workspace_runtime_state(profile_id, hot_dirty, workspace_id, subject_id);

CREATE INDEX IF NOT EXISTS idx_runtime_state_dream_dirty
ON workspace_runtime_state(profile_id, dream_dirty, workspace_id, subject_id);

-- Existing stores should rebuild their first runtime projections after the
-- upgrade instead of assuming that an old snapshot is compatible.
INSERT OR IGNORE INTO workspace_runtime_state(
    profile_id, workspace_id, subject_id, agent_id, hot_dirty, dream_dirty
)
SELECT
    profile_id,
    workspace_id,
    subject_id,
    CASE
        WHEN visibility_scope='agent' THEN COALESCE(owner_agent_id, origin_agent_id, '')
        ELSE ''
    END,
    1,
    1
FROM claims
WHERE COALESCE(profile_id, '') != ''
  AND COALESCE(workspace_id, '') != ''
  AND COALESCE(subject_id, '') != '';

INSERT OR IGNORE INTO workspace_runtime_state(
    profile_id, workspace_id, subject_id, agent_id, hot_dirty, dream_dirty
)
SELECT
    profile_id,
    workspace_id,
    subject_id,
    '',
    1,
    1
FROM raw_events
WHERE COALESCE(profile_id, '') != ''
  AND COALESCE(workspace_id, '') != ''
  AND COALESCE(subject_id, '') != '';

-- Replace the historical global uniqueness rule.  The semantic key remains
-- compatible with old hashes, while the database now enforces it per user and
-- project scope so identical technical facts may exist in separate projects.
DROP INDEX IF EXISTS idx_active_claim_semantic_key;
DROP INDEX IF EXISTS idx_claims_subject_hash;

CREATE UNIQUE INDEX IF NOT EXISTS idx_active_claim_semantic_scope
ON claims(profile_id, workspace_id, subject_id, semantic_key)
WHERE status='active' AND semantic_key IS NOT NULL AND semantic_key!='';

CREATE INDEX IF NOT EXISTS idx_claims_scope_hash
ON claims(profile_id, workspace_id, subject_id, content_hash);
