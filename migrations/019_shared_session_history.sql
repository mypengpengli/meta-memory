-- Completed session cards are the cross-Agent history boundary.  The source
-- sessions themselves remain separate; only explicitly bounded summaries and
-- detail visibility are shared in a workspace.

ALTER TABLE session_cards ADD COLUMN tool_summary TEXT NOT NULL DEFAULT '';
ALTER TABLE session_cards ADD COLUMN completed_turn_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE session_cards ADD COLUMN last_completed_turn_at TEXT;
ALTER TABLE session_cards ADD COLUMN summary_visibility TEXT NOT NULL DEFAULT 'workspace';
ALTER TABLE session_cards ADD COLUMN detail_visibility TEXT NOT NULL DEFAULT 'workspace';
ALTER TABLE session_cards ADD COLUMN summary_generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE session_cards ADD COLUMN summary_dirty INTEGER NOT NULL DEFAULT 1;

-- These fields reserve per-session privacy controls for a future private
-- session mode.  This release deliberately keeps the default workspace-wide.
ALTER TABLE sessions ADD COLUMN summary_visibility TEXT NOT NULL DEFAULT 'workspace';
ALTER TABLE sessions ADD COLUMN detail_visibility TEXT NOT NULL DEFAULT 'workspace';

CREATE INDEX IF NOT EXISTS idx_session_cards_shared_history
ON session_cards(profile_id, workspace_id, subject_id, summary_visibility, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_turns_completed_session_scope
ON turns(profile_id, workspace_id, subject_id, origin_agent_id, external_session_id, status, completed_at);

-- Existing cards may contain an incomplete historical tail.  A heartbeat
-- rebuilds them before they are considered shared history.
UPDATE session_cards SET summary_dirty=1 WHERE summary_generation=0;
