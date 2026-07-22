-- Shared-world memory primitives for people, households, projects, devices,
-- sessions, and time-bounded events.  These tables deliberately complement
-- the existing Claim/evidence pipeline instead of changing or copying Claims.

CREATE TABLE IF NOT EXISTS memory_audiences (
    audience_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    audience_type TEXT NOT NULL,
    audience_key TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(profile_id, audience_type, audience_key)
);
CREATE INDEX IF NOT EXISTS idx_memory_audiences_profile
ON memory_audiences(profile_id, status, audience_type, audience_key);

CREATE TABLE IF NOT EXISTS memory_audience_members (
    audience_id TEXT NOT NULL,
    member_type TEXT NOT NULL,
    member_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(audience_id, member_type, member_id),
    FOREIGN KEY(audience_id) REFERENCES memory_audiences(audience_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_memory_audience_members_lookup
ON memory_audience_members(member_type, member_id, audience_id);

CREATE TABLE IF NOT EXISTS memory_channels (
    channel_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    channel_type TEXT NOT NULL,
    channel_key TEXT NOT NULL,
    audience_id TEXT NOT NULL,
    subject_id TEXT NOT NULL DEFAULT '',
    workspace_id TEXT NOT NULL DEFAULT '',
    owner_agent_id TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(profile_id, channel_type, channel_key),
    FOREIGN KEY(audience_id) REFERENCES memory_audiences(audience_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_channels_audience
ON memory_channels(profile_id, audience_id, status, channel_type);
CREATE INDEX IF NOT EXISTS idx_memory_channels_workspace
ON memory_channels(profile_id, workspace_id, status);

-- This is an intentionally curated cross-workspace feed.  Agents publish a
-- compact result here; raw conversations and device telemetry stay in their
-- original workspace/device scope.
CREATE TABLE IF NOT EXISTS shared_activities (
    activity_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    source_workspace_id TEXT NOT NULL DEFAULT '',
    subject_id TEXT NOT NULL DEFAULT '',
    source_agent_id TEXT NOT NULL DEFAULT '',
    source_session_id TEXT NOT NULL DEFAULT '',
    activity_kind TEXT NOT NULL DEFAULT 'update',
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    importance REAL NOT NULL DEFAULT 0.5,
    occurred_at TEXT NOT NULL,
    valid_until TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    supersedes_activity_id TEXT,
    superseded_by_activity_id TEXT,
    idempotency_key TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(channel_id) REFERENCES memory_channels(channel_id),
    FOREIGN KEY(supersedes_activity_id) REFERENCES shared_activities(activity_id),
    FOREIGN KEY(superseded_by_activity_id) REFERENCES shared_activities(activity_id)
);
CREATE INDEX IF NOT EXISTS idx_shared_activities_feed
ON shared_activities(profile_id, channel_id, status, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_shared_activities_workspace
ON shared_activities(profile_id, source_workspace_id, occurred_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_shared_activities_idempotency
ON shared_activities(profile_id, source_agent_id, idempotency_key)
WHERE idempotency_key IS NOT NULL AND idempotency_key!='';

-- Temporal state is separate from durable Claims.  One current state exists
-- per channel/subject/key; newer observations supersede it and stale uploads
-- are retained as history without replacing the current value.
CREATE TABLE IF NOT EXISTS temporal_states (
    state_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    state_key TEXT NOT NULL,
    value_json TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL DEFAULT '',
    source_workspace_id TEXT NOT NULL DEFAULT '',
    source_agent_id TEXT NOT NULL DEFAULT '',
    source_ref TEXT NOT NULL DEFAULT '',
    confidence REAL,
    observed_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    supersedes_state_id TEXT,
    superseded_by_state_id TEXT,
    idempotency_key TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(channel_id) REFERENCES memory_channels(channel_id),
    FOREIGN KEY(supersedes_state_id) REFERENCES temporal_states(state_id),
    FOREIGN KEY(superseded_by_state_id) REFERENCES temporal_states(state_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_temporal_states_current
ON temporal_states(profile_id, channel_id, subject_id, state_key)
WHERE status='active';
CREATE INDEX IF NOT EXISTS idx_temporal_states_history
ON temporal_states(profile_id, channel_id, subject_id, state_key, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_temporal_states_expiry
ON temporal_states(profile_id, status, valid_until);
CREATE UNIQUE INDEX IF NOT EXISTS idx_temporal_states_idempotency
ON temporal_states(profile_id, source_agent_id, idempotency_key)
WHERE idempotency_key IS NOT NULL AND idempotency_key!='';

-- Bytes live under memory-data/assets/objects.  SQLite stores only immutable
-- metadata and a store-relative path; the physical filename is content based
-- and never derived from a client supplied filename.
CREATE TABLE IF NOT EXISTS binary_assets (
    asset_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    byte_size INTEGER NOT NULL,
    object_path TEXT NOT NULL,
    original_name TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(profile_id, sha256)
);
CREATE INDEX IF NOT EXISTS idx_binary_assets_listing
ON binary_assets(profile_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_binary_assets_object
ON binary_assets(object_path, status);

-- A stable map_id owns monotonically increasing immutable versions.  A map
-- version may point at an asset (occupancy grid, point cloud, etc.) but its
-- searchable topology/metadata stays ordinary JSON.
CREATE TABLE IF NOT EXISTS spatial_maps (
    map_version_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    map_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    coordinate_frame TEXT NOT NULL,
    asset_id TEXT,
    source_workspace_id TEXT NOT NULL DEFAULT '',
    source_agent_id TEXT NOT NULL DEFAULT '',
    captured_at TEXT NOT NULL,
    previous_version_id TEXT,
    idempotency_key TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(profile_id, map_id, version),
    FOREIGN KEY(channel_id) REFERENCES memory_channels(channel_id),
    FOREIGN KEY(asset_id) REFERENCES binary_assets(asset_id),
    FOREIGN KEY(previous_version_id) REFERENCES spatial_maps(map_version_id)
);
CREATE INDEX IF NOT EXISTS idx_spatial_maps_latest
ON spatial_maps(profile_id, map_id, status, version DESC);
CREATE INDEX IF NOT EXISTS idx_spatial_maps_channel
ON spatial_maps(profile_id, channel_id, status, map_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_spatial_maps_idempotency
ON spatial_maps(profile_id, source_agent_id, idempotency_key)
WHERE idempotency_key IS NOT NULL AND idempotency_key!='';

CREATE TABLE IF NOT EXISTS spatial_observations (
    observation_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL DEFAULT '',
    subject_id TEXT NOT NULL DEFAULT '',
    source_agent_id TEXT NOT NULL DEFAULT '',
    owner_agent_id TEXT NOT NULL DEFAULT '',
    map_version_id TEXT,
    asset_id TEXT,
    location_id TEXT NOT NULL DEFAULT '',
    location_text TEXT NOT NULL DEFAULT '',
    caption TEXT NOT NULL DEFAULT '',
    ocr_text TEXT NOT NULL DEFAULT '',
    objects_json TEXT NOT NULL DEFAULT '[]',
    search_text TEXT NOT NULL DEFAULT '',
    confidence REAL,
    observed_at TEXT NOT NULL,
    valid_until TEXT,
    visibility_scope TEXT NOT NULL DEFAULT 'channel',
    status TEXT NOT NULL DEFAULT 'active',
    supersedes_observation_id TEXT,
    superseded_by_observation_id TEXT,
    idempotency_key TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(channel_id) REFERENCES memory_channels(channel_id),
    FOREIGN KEY(map_version_id) REFERENCES spatial_maps(map_version_id),
    FOREIGN KEY(asset_id) REFERENCES binary_assets(asset_id),
    FOREIGN KEY(supersedes_observation_id) REFERENCES spatial_observations(observation_id),
    FOREIGN KEY(superseded_by_observation_id) REFERENCES spatial_observations(observation_id)
);
CREATE INDEX IF NOT EXISTS idx_spatial_observations_listing
ON spatial_observations(profile_id, channel_id, status, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_spatial_observations_map
ON spatial_observations(profile_id, map_version_id, status, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_spatial_observations_location
ON spatial_observations(profile_id, location_id, status, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_spatial_observations_expiry
ON spatial_observations(profile_id, status, valid_until);
CREATE UNIQUE INDEX IF NOT EXISTS idx_spatial_observations_idempotency
ON spatial_observations(profile_id, source_agent_id, idempotency_key)
WHERE idempotency_key IS NOT NULL AND idempotency_key!='';
