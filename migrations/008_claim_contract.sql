-- Claims are the source of truth.  Markdown/documents are projections and
-- must never be able to resurrect an obsolete claim during reindexing.
ALTER TABLE claims ADD COLUMN domain TEXT NOT NULL DEFAULT 'general';
ALTER TABLE claims ADD COLUMN predicate TEXT DEFAULT '';
ALTER TABLE claims ADD COLUMN subject_text TEXT DEFAULT '';
ALTER TABLE claims ADD COLUMN object_text TEXT DEFAULT '';
ALTER TABLE claims ADD COLUMN qualifiers_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE claims ADD COLUMN durability REAL NOT NULL DEFAULT 0.5;
ALTER TABLE claims ADD COLUMN confirmed_utility REAL NOT NULL DEFAULT 0.0;
ALTER TABLE claims ADD COLUMN replaced_by TEXT;
ALTER TABLE claims ADD COLUMN corrected_by TEXT;
ALTER TABLE claims ADD COLUMN supersedes TEXT;
ALTER TABLE claims ADD COLUMN security_state TEXT NOT NULL DEFAULT 'clean';
ALTER TABLE claims ADD COLUMN security_findings_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE claims ADD COLUMN prompt_eligible INTEGER NOT NULL DEFAULT 1;

ALTER TABLE memory_units ADD COLUMN domain TEXT NOT NULL DEFAULT 'general';
ALTER TABLE memory_units ADD COLUMN predicate TEXT DEFAULT '';
ALTER TABLE memory_units ADD COLUMN subject_text TEXT DEFAULT '';
ALTER TABLE memory_units ADD COLUMN object_text TEXT DEFAULT '';
ALTER TABLE memory_units ADD COLUMN qualifiers_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE memory_units ADD COLUMN valid_from TEXT;
ALTER TABLE memory_units ADD COLUMN valid_to TEXT;
ALTER TABLE memory_units ADD COLUMN observed_at TEXT;
ALTER TABLE memory_units ADD COLUMN durability REAL NOT NULL DEFAULT 0.5;
ALTER TABLE memory_units ADD COLUMN entities_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE memory_units ADD COLUMN security_state TEXT NOT NULL DEFAULT 'clean';
ALTER TABLE memory_units ADD COLUMN security_findings_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE documents ADD COLUMN valid_from TEXT;
ALTER TABLE documents ADD COLUMN valid_to TEXT;
ALTER TABLE documents ADD COLUMN verification_state TEXT;
ALTER TABLE documents ADD COLUMN security_state TEXT NOT NULL DEFAULT 'clean';
ALTER TABLE documents ADD COLUMN prompt_eligible INTEGER NOT NULL DEFAULT 1;

CREATE INDEX IF NOT EXISTS idx_claims_structured ON claims(subject_id, predicate, object_text, status);
CREATE INDEX IF NOT EXISTS idx_claims_prompt_eligible ON claims(subject_id, status, prompt_eligible, security_state);
