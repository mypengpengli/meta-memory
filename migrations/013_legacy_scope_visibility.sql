-- Rows that existed before 2.2 had no meaningful workspace/agent label.
-- Preserve their historical profile-wide behavior explicitly instead of
-- silently hiding them behind the synthetic `global` workspace value.
UPDATE raw_events
SET visibility_scope='global'
WHERE profile_id='default' AND workspace_id='global' AND origin_agent_id='' AND visibility_scope='workspace';
UPDATE memory_units
SET visibility_scope='global'
WHERE profile_id='default' AND workspace_id='global' AND origin_agent_id='' AND visibility_scope='workspace';
UPDATE claims
SET visibility_scope='global'
WHERE profile_id='default' AND workspace_id='global' AND origin_agent_id='' AND visibility_scope='workspace';
UPDATE documents
SET visibility_scope='global'
WHERE profile_id='default' AND workspace_id='global' AND (owner_agent_id IS NULL OR owner_agent_id='') AND visibility_scope='workspace';
UPDATE chunks
SET visibility_scope='global'
WHERE profile_id='default' AND workspace_id='global' AND (owner_agent_id IS NULL OR owner_agent_id='') AND visibility_scope='workspace';
