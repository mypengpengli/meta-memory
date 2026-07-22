-- Correctness and scope metadata for hosted shared-world assets/observations.
-- Object bytes remain deduplicated by binary_assets; each upload gets an
-- explicit access binding so the same SHA-256 object can safely be referenced
-- from more than one channel/workspace without widening either one.

CREATE TABLE IF NOT EXISTS binary_asset_scopes (
    scope_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    visibility_scope TEXT NOT NULL DEFAULT 'profile',
    channel_id TEXT NOT NULL DEFAULT '',
    workspace_id TEXT NOT NULL DEFAULT '',
    owner_agent_id TEXT NOT NULL DEFAULT '',
    source_subject_id TEXT NOT NULL DEFAULT '',
    source_agent_id TEXT NOT NULL DEFAULT '',
    original_name TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(asset_id, visibility_scope, channel_id, workspace_id, owner_agent_id),
    FOREIGN KEY(asset_id) REFERENCES binary_assets(asset_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_binary_asset_scopes_lookup
ON binary_asset_scopes(profile_id, visibility_scope, channel_id, workspace_id, owner_agent_id, asset_id);

-- Assets created before explicit bindings were profile-visible.  Preserve
-- that behavior instead of silently hiding data after upgrade.
INSERT OR IGNORE INTO binary_asset_scopes(
    scope_id,asset_id,profile_id,visibility_scope,original_name,metadata_json,created_at
)
SELECT lower(hex(randomblob(16))),asset_id,profile_id,'profile',original_name,metadata_json,created_at
FROM binary_assets;

ALTER TABLE spatial_observations ADD COLUMN observation_kind TEXT NOT NULL DEFAULT 'spatial_observation';
ALTER TABLE spatial_observations ADD COLUMN source_ref TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_spatial_observations_source
ON spatial_observations(profile_id, source_agent_id, source_ref, observed_at DESC);

ALTER TABLE shared_activities ADD COLUMN source_ref TEXT NOT NULL DEFAULT '';
ALTER TABLE shared_activities ADD COLUMN confidence REAL;
CREATE INDEX IF NOT EXISTS idx_shared_activities_source
ON shared_activities(profile_id, source_agent_id, source_ref, occurred_at DESC);
