"""Non-mutating health report for a Meta Memory store."""
from __future__ import annotations

from _common import open_db, utc_now


def doctor(root, *, mode: str = "full") -> dict[str, object]:
    conn = open_db(root)
    migrations = [str(row[0]) for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    fts = "session_messages_fts" in tables and "document_fts" in tables
    blocked = int(conn.execute("SELECT COUNT(*) FROM claims WHERE security_state='blocked'").fetchone()[0])
    inconsistent = int(conn.execute("SELECT COUNT(*) FROM (SELECT c.id FROM claims c LEFT JOIN claim_sources s ON s.claim_id=c.id WHERE c.status='active' GROUP BY c.id HAVING COUNT(s.raw_event_id)=0)").fetchone()[0]) if "claims" in tables else 0
    pending_jobs = int(conn.execute("SELECT COUNT(*) FROM review_jobs WHERE status IN ('pending','running','failed')").fetchone()[0]) if "review_jobs" in tables else 0
    pending_projections = int(conn.execute("SELECT COUNT(*) FROM projection_outbox WHERE status IN ('pending','running','failed')").fetchone()[0]) if "projection_outbox" in tables else 0
    locks = int(conn.execute("SELECT COUNT(*) FROM runtime_locks WHERE leased_until>?", (utc_now(),)).fetchone()[0]) if "runtime_locks" in tables else 0
    recent = conn.execute("SELECT MAX(last_maintained_at) FROM workspace_runtime_state").fetchone()[0] if "workspace_runtime_state" in tables else None
    conn.close()
    result = {"status": "ok" if not inconsistent else "warning", "mode": mode, "migrations": migrations, "fts_available": fts, "blocked_claims": blocked, "active_claims_without_sources": inconsistent, "pending_jobs": pending_jobs, "pending_projections": pending_projections, "active_runtime_locks": locks, "last_maintained_at": recent}
    return result
