"""The single, lease-protected local maintenance entry point."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .config import AppConfig
from .legacy import bootstrap
from .runtime_locks import acquire, inspect, release, renew


def _dirty_hot(config: AppConfig) -> list[dict[str, object]]:
    bootstrap()
    from _common import open_db

    conn = open_db(Path(config.store))
    try:
        rows = conn.execute(
            """
            SELECT profile_id,workspace_id,subject_id,agent_id,claim_generation
            FROM workspace_runtime_state
            WHERE profile_id=? AND hot_dirty=1
            ORDER BY updated_at,workspace_id,subject_id
            """,
            (config.profile_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"profile_id": str(row[0]), "workspace_id": str(row[1]), "subject_id": str(row[2]), "agent_id": str(row[3] or ""), "generation": int(row[4] or 0)}
        for row in rows
    ]


def _refresh_dirty_hot(config: AppConfig) -> list[dict[str, object]]:
    bootstrap()
    from _common import open_db, utc_now
    from build_hot_memory import build_hot_memory

    results: list[dict[str, object]] = []
    for scope in _dirty_hot(config):
        try:
            snapshot = build_hot_memory(
                Path(config.store),
                subject_id=str(scope["subject_id"]),
                profile_id=str(scope["profile_id"]),
                workspace_id=str(scope["workspace_id"]),
                agent_id=str(scope["agent_id"]),
            )
            conn = open_db(Path(config.store))
            try:
                # Do not clear a new dirty generation that appeared while the
                # snapshot was being built.
                changed = conn.execute(
                    """
                    UPDATE workspace_runtime_state
                    SET hot_dirty=CASE WHEN claim_generation=? THEN 0 ELSE 1 END,
                        hot_generation=CASE WHEN claim_generation=? THEN ? ELSE hot_generation END,
                        last_maintained_at=?,last_success_at=?,last_error=NULL,updated_at=?
                    WHERE profile_id=? AND workspace_id=? AND subject_id=? AND agent_id=?
                    """,
                    (
                        scope["generation"], scope["generation"], scope["generation"], utc_now(), utc_now(), utc_now(),
                        scope["profile_id"], scope["workspace_id"], scope["subject_id"], scope["agent_id"],
                    ),
                ).rowcount
                conn.commit()
            finally:
                conn.close()
            results.append({"scope": scope, "snapshot": snapshot, "state_updated": bool(changed)})
        except Exception as exc:
            results.append({"scope": scope, "status": "error", "error": str(exc)})
    return results


def _record_maintenance(config: AppConfig, *, error: str = "") -> None:
    bootstrap()
    from _common import open_db, utc_now

    conn = open_db(Path(config.store))
    try:
        conn.execute(
            """
            UPDATE workspace_runtime_state
            SET last_maintained_at=?,last_success_at=CASE WHEN ?='' THEN ? ELSE last_success_at END,
                last_error=CASE WHEN ?='' THEN NULL ELSE ? END,updated_at=?
            WHERE profile_id=?
            """,
            (utc_now(), error, utc_now(), error, error[:1000], utc_now(), config.profile_id),
        )
        conn.commit()
    finally:
        conn.close()


def maintain(config: AppConfig, *, max_jobs: int = 20) -> dict[str, Any]:
    """Recover durable work in a fixed order without concurrent rebuilds."""
    bootstrap()
    from _common import ensure_store_ready
    from background_review import recover_stuck_jobs, run_pending
    from build_hot_memory import garbage_collect_snapshots
    from doctor import doctor
    from projection_outbox import process_projection_outbox, recover_stuck_outbox
    from .spool import replay_spool

    root = Path(config.store)
    initialized = ensure_store_ready(root)
    lease = acquire(root, f"maintain:{config.profile_id}", owner_id=f"maintain:{uuid.uuid4()}", lease_seconds=600)
    if not lease.acquired:
        return {
            "status": "skipped",
            "reason": "maintenance_already_running",
            "lock": {"owner_id": lease.owner_id, "leased_until": lease.leased_until},
        }
    try:
        spool = replay_spool(config, limit=max(20, max_jobs * 2))
        renew(root, lease)
        recovered_review = recover_stuck_jobs(root)
        recovered_projections = recover_stuck_outbox(root)
        review = run_pending(root, max_jobs=max(1, max_jobs), policy="balanced", memory_mode=config.memory_mode)
        renew(root, lease)
        projections = process_projection_outbox(root, limit=max(100, max_jobs * 20))
        renew(root, lease)
        hot = _refresh_dirty_hot(config)
        snapshot_gc = garbage_collect_snapshots(root)
        health = doctor(root, mode="quick")
        _record_maintenance(config)
        return {
            "status": "ok",
            "initialized": initialized,
            "spool": spool,
            "recovered_review_jobs": recovered_review,
            "recovered_projections": recovered_projections,
            "review": review,
            "projections": projections,
            "hot_memory": hot,
            "snapshot_gc": snapshot_gc,
            "health": health,
        }
    except Exception as exc:
        _record_maintenance(config, error=str(exc))
        raise
    finally:
        release(root, lease)


def status(config: AppConfig) -> dict[str, Any]:
    bootstrap()
    from _common import ensure_store_ready, open_db
    from doctor import doctor

    ensure_store_ready(Path(config.store))
    conn = open_db(config.store)
    try:
        counts = {
            "raw_events": int(conn.execute("SELECT COUNT(*) FROM raw_events WHERE profile_id=?", (config.profile_id,)).fetchone()[0]),
            "claims": int(conn.execute("SELECT COUNT(*) FROM claims WHERE profile_id=?", (config.profile_id,)).fetchone()[0]),
            "pending_review_jobs": int(conn.execute("SELECT COUNT(*) FROM review_jobs WHERE status IN ('pending','running','failed')").fetchone()[0]),
            "pending_projections": int(conn.execute("SELECT COUNT(*) FROM projection_outbox WHERE status IN ('pending','running','failed')").fetchone()[0]),
            "dirty_hot_scopes": int(conn.execute("SELECT COUNT(*) FROM workspace_runtime_state WHERE profile_id=? AND hot_dirty=1", (config.profile_id,)).fetchone()[0]),
            "dirty_dream_scopes": int(conn.execute("SELECT COUNT(*) FROM workspace_runtime_state WHERE profile_id=? AND dream_dirty=1", (config.profile_id,)).fetchone()[0]),
        }
    finally:
        conn.close()
    return {
        "status": "ok", "user": config.user_name, "store": str(config.store),
        "maintenance_enabled": config.maintenance_enabled, "dream_enabled": config.dream_enabled,
        "counts": counts, "maintenance_lock": inspect(config.store, f"maintain:{config.profile_id}"),
        "health": doctor(Path(config.store), mode="quick"),
    }
