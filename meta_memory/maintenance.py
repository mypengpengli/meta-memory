"""The one scheduled maintenance entry point for the local shared store."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import AppConfig
from .legacy import bootstrap


def _workspaces(config: AppConfig) -> list[str]:
    bootstrap()
    from _common import open_db

    conn = open_db(config.store)
    rows = conn.execute("SELECT DISTINCT workspace_id FROM claims WHERE profile_id=? UNION SELECT DISTINCT workspace_id FROM raw_events WHERE profile_id=?", (config.profile_id, config.profile_id)).fetchall()
    conn.close()
    values = {str(row[0]) for row in rows if str(row[0] or "")}
    values.add(f"project:{config.default_project}")
    return sorted(values)


def maintain(config: AppConfig, *, max_jobs: int = 20) -> dict[str, Any]:
    """Recover work, organize queued turns, project indexes, and check health.

    This replaces the two permanent worker processes in the default local
    deployment.  A lease still protects the work when two timers overlap.
    """
    bootstrap()
    from _common import ensure_store_ready
    from background_review import recover_stuck_jobs, run_pending
    from build_hot_memory import build_hot_memory, garbage_collect_snapshots
    from doctor import doctor
    from projection_outbox import process_projection_outbox

    initialized = ensure_store_ready(Path(config.store))
    recovered = recover_stuck_jobs(Path(config.store))
    review = run_pending(Path(config.store), max_jobs=max(1, max_jobs), policy="balanced", apply_low_risk=config.auto_memory)
    projections = process_projection_outbox(Path(config.store), limit=max(100, max_jobs * 20))
    hot = [
        build_hot_memory(Path(config.store), subject_id=config.subject_id, profile_id=config.profile_id, workspace_id=workspace, agent_id="")
        for workspace in _workspaces(config)
    ]
    return {
        "status": "ok", "initialized": initialized, "recovered_review_jobs": recovered,
        "review": review, "projections": projections,
        "hot_memory": hot, "snapshot_gc": garbage_collect_snapshots(Path(config.store)),
        "health": doctor(Path(config.store)),
    }


def status(config: AppConfig) -> dict[str, Any]:
    bootstrap()
    from _common import ensure_store_ready, open_db
    from doctor import doctor

    ensure_store_ready(Path(config.store))
    conn = open_db(config.store)
    counts = {
        "raw_events": int(conn.execute("SELECT COUNT(*) FROM raw_events WHERE profile_id=?", (config.profile_id,)).fetchone()[0]),
        "claims": int(conn.execute("SELECT COUNT(*) FROM claims WHERE profile_id=?", (config.profile_id,)).fetchone()[0]),
        "pending_review_jobs": int(conn.execute("SELECT COUNT(*) FROM review_jobs WHERE status IN ('pending','running','failed')").fetchone()[0]),
        "pending_projections": int(conn.execute("SELECT COUNT(*) FROM projection_outbox WHERE status IN ('pending','running','failed')").fetchone()[0]),
    }
    conn.close()
    return {
        "status": "ok", "user": config.user_name, "store": str(config.store),
        "maintenance_enabled": config.maintenance_enabled, "dream_enabled": config.dream_enabled,
        "counts": counts, "health": doctor(Path(config.store)),
    }
