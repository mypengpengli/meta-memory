-- Persistent work queue and reviewable writes.  Jobs are durable before a
-- background worker touches them, which makes restart recovery deterministic.
CREATE TABLE IF NOT EXISTS review_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uid TEXT NOT NULL UNIQUE,
    job_key TEXT NOT NULL UNIQUE,
    subject_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    event_start_id INTEGER NOT NULL DEFAULT 0,
    event_end_id INTEGER NOT NULL DEFAULT 0,
    trigger_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    reviewer_model TEXT,
    input_digest TEXT,
    memory_plan_json TEXT,
    skill_plan_json TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    completed_at TEXT,
    next_retry_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_jobs_status ON review_jobs(status, next_retry_at, created_at);
CREATE INDEX IF NOT EXISTS idx_review_jobs_subject ON review_jobs(subject_id, session_id, created_at);

CREATE TABLE IF NOT EXISTS write_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_uid TEXT NOT NULL UNIQUE,
    subject_id TEXT NOT NULL,
    session_id TEXT,
    origin TEXT NOT NULL,
    action TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    diff_text TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TEXT,
    reviewed_by TEXT,
    review_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_write_proposals_pending ON write_proposals(status, subject_id, created_at);

CREATE TABLE IF NOT EXISTS skill_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_uid TEXT NOT NULL UNIQUE,
    subject_id TEXT NOT NULL,
    origin TEXT NOT NULL,
    action TEXT NOT NULL,
    skill TEXT,
    section TEXT,
    plan_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    diff_text TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TEXT,
    reviewed_by TEXT,
    review_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_skill_proposals_pending ON skill_proposals(status, subject_id, created_at);
