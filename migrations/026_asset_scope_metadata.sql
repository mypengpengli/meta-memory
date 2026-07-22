-- Preserve upload-specific media metadata alongside each explicit asset
-- access scope while continuing to deduplicate the underlying object bytes.

ALTER TABLE binary_asset_scopes ADD COLUMN media_type TEXT NOT NULL DEFAULT 'application/octet-stream';
UPDATE binary_asset_scopes
SET media_type=COALESCE(
    (SELECT a.media_type FROM binary_assets a WHERE a.asset_id=binary_asset_scopes.asset_id),
    'application/octet-stream'
);
