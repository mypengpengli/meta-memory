-- Agent-private claims may share wording with another Agent's private claim.
-- Keep the normal shared semantic identity unchanged while adding the owner to
-- the database uniqueness boundary for private visibility.

DROP INDEX IF EXISTS idx_active_claim_semantic_scope;

CREATE UNIQUE INDEX IF NOT EXISTS idx_active_claim_semantic_scope
ON claims(
    profile_id,
    workspace_id,
    subject_id,
    visibility_scope,
    IFNULL(owner_agent_id, ''),
    semantic_key
)
WHERE status='active' AND semantic_key IS NOT NULL AND semantic_key!='';
