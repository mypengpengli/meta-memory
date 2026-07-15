"""The single, lease-protected local maintenance entry point."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .config import AppConfig
from .legacy import bootstrap
from .runtime_locks import acquire, inspect, release, renew


def _dirty_hot(config: AppConfig, *, limit: int = 20) -> list[dict[str, object]]:
    bootstrap()
    from _common import open_db

    conn = open_db(Path(config.store))
    try:
        rows = conn.execute(
            """
            SELECT profile_id,workspace_id,subject_id,agent_id,claim_generation
            FROM workspace_runtime_state
            WHERE profile_id=? AND hot_dirty=1
            ORDER BY updated_at,workspace_id,subject_id LIMIT ?
            """,
            (config.profile_id, max(1, limit)),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"profile_id": str(row[0]), "workspace_id": str(row[1]), "subject_id": str(row[2]), "agent_id": str(row[3] or ""), "generation": int(row[4] or 0)}
        for row in rows
    ]


def _refresh_dirty_hot(config: AppConfig, *, max_scopes: int = 20) -> list[dict[str, object]]:
    bootstrap()
    from _common import open_db, utc_now
    from build_hot_memory import build_hot_memory

    results: list[dict[str, object]] = []
    for scope in _dirty_hot(config, limit=max_scopes):
        try:
            snapshot = build_hot_memory(
                Path(config.store),
                subject_id=str(scope["subject_id"]),
                profile_id=str(scope["profile_id"]),
                workspace_id=str(scope["workspace_id"]),
                agent_id=str(scope["agent_id"]),
                generation=int(scope["generation"]),
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
                conn.execute(
                    "UPDATE hot_snapshots SET generation=?,refreshed_at=? WHERE snapshot_uid=?",
                    (scope["generation"], utc_now(), snapshot["snapshot_uid"]),
                )
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


def _pending_work(config: AppConfig) -> dict[str, int]:
    """Check durable work queues without scanning claims or rebuilding output."""

    bootstrap()
    from _common import open_db
    from .spool import pending_dir

    conn = open_db(Path(config.store))
    try:
        work = {
            "review_jobs": int(conn.execute("SELECT COUNT(*) FROM review_jobs WHERE status IN ('pending','running','failed')").fetchone()[0]),
            "projections": int(conn.execute("SELECT COUNT(*) FROM projection_outbox WHERE status IN ('pending','running','failed')").fetchone()[0]),
            "summary_scopes": int(conn.execute("SELECT COUNT(*) FROM session_cards WHERE profile_id=? AND summary_dirty=1", (config.profile_id,)).fetchone()[0]),
            "hot_scopes": int(conn.execute("SELECT COUNT(*) FROM workspace_runtime_state WHERE profile_id=? AND hot_dirty=1", (config.profile_id,)).fetchone()[0]),
            "dream_scopes": int(conn.execute("SELECT COUNT(*) FROM workspace_runtime_state WHERE profile_id=? AND dream_dirty=1", (config.profile_id,)).fetchone()[0]),
        }
    finally:
        conn.close()
    work["spool"] = len(list(pending_dir(config).glob("*.json"))) if pending_dir(config).is_dir() else 0
    return work


def _record_heartbeat_state(config: AppConfig, *, status: str, work: dict[str, int], processed_turns: int = 0, updated_claims: int = 0, updated_sessions: int = 0, new_snapshots: int = 0, error: str = "") -> None:
    bootstrap()
    from _common import open_db, utc_now

    conn = open_db(Path(config.store))
    try:
        conn.execute(
            """
            INSERT INTO dream_runtime_state(
                profile_id,workspace_id,heartbeat_last_started_at,heartbeat_last_completed_at,
                heartbeat_last_status,heartbeat_last_error,heartbeat_last_dirty_scopes,
                heartbeat_last_processed_turns,heartbeat_last_updated_claims,
                heartbeat_last_updated_sessions,heartbeat_last_new_snapshots,updated_at
            ) VALUES(?, '*', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id,workspace_id) DO UPDATE SET
                heartbeat_last_started_at=excluded.heartbeat_last_started_at,
                heartbeat_last_completed_at=excluded.heartbeat_last_completed_at,
                heartbeat_last_status=excluded.heartbeat_last_status,
                heartbeat_last_error=excluded.heartbeat_last_error,
                heartbeat_last_dirty_scopes=excluded.heartbeat_last_dirty_scopes,
                heartbeat_last_processed_turns=excluded.heartbeat_last_processed_turns,
                heartbeat_last_updated_claims=excluded.heartbeat_last_updated_claims,
                heartbeat_last_updated_sessions=excluded.heartbeat_last_updated_sessions,
                heartbeat_last_new_snapshots=excluded.heartbeat_last_new_snapshots,
                updated_at=excluded.updated_at
            """,
            (
                config.profile_id, utc_now(), utc_now(), status, error[:2000] or None,
                sum(work.values()), processed_turns, updated_claims, updated_sessions, new_snapshots, utc_now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def maintain(config: AppConfig, *, max_jobs: int = 20, max_scopes: int | None = None) -> dict[str, Any]:
    """Run the incremental Dream heartbeat; keep the legacy name compatible."""
    bootstrap()
    from _common import ensure_store_ready
    from background_review import recover_stuck_jobs, run_pending
    from build_hot_memory import garbage_collect_snapshots
    from build_session_card import build_cards, refresh_dirty_cards
    from doctor import doctor
    from projection_outbox import process_projection_outbox, recover_stuck_outbox
    from .spool import replay_spool

    root = Path(config.store)
    initialized = ensure_store_ready(root)
    scope_limit = max(1, int(max_scopes or getattr(config, "dream_heartbeat_max_scopes", 20)))
    job_limit = max(1, min(int(max_jobs), int(getattr(config, "dream_heartbeat_max_jobs", max_jobs))))
    work_before = _pending_work(config)
    lease = acquire(root, f"maintain:{config.profile_id}", owner_id=f"maintain:{uuid.uuid4()}", lease_seconds=600)
    if not lease.acquired:
        return {
            "status": "skipped",
            "reason": "maintenance_already_running",
            "lock": {"owner_id": lease.owner_id, "leased_until": lease.leased_until},
        }
    try:
        heartbeat_work = {key: value for key, value in work_before.items() if key != "dream_scopes"}
        if not any(heartbeat_work.values()):
            _record_heartbeat_state(config, status="idle", work=work_before)
            return {"status": "idle", "dirty_scopes": sum(heartbeat_work.values()), "processed_turns": 0, "updated_claims": 0, "updated_sessions": 0, "new_snapshots": 0, "work": work_before}
        spool = replay_spool(config, limit=max(20, job_limit * 2))
        renew(root, lease)
        recovered_review = recover_stuck_jobs(root)
        recovered_projections = recover_stuck_outbox(root)
        review = run_pending(root, max_jobs=job_limit, policy="balanced", memory_mode=config.memory_mode)
        renew(root, lease)
        cards = build_cards(root, profile_id=config.profile_id, force=True, max_events=max(20, job_limit * 4))
        refreshed_cards = refresh_dirty_cards(root, profile_id=config.profile_id, limit=scope_limit)
        renew(root, lease)
        projections = process_projection_outbox(root, limit=max(100, job_limit * 20))
        renew(root, lease)
        hot = _refresh_dirty_hot(config, max_scopes=scope_limit)
        snapshot_gc = garbage_collect_snapshots(root)
        health = doctor(root, mode="quick")
        _record_maintenance(config)
        processed_turns = sum(1 for item in review.get("results", []) if str(item.get("status")) in {"applied", "staged", "planned"})
        updated_claims = sum(len(item.get("unit_ids", [])) for item in review.get("results", []))
        updated_sessions = len([item for item in cards.get("cards", []) if item.get("card_id")]) + len(refreshed_cards.get("cards", []))
        new_snapshots = sum(1 for item in hot if bool(item.get("snapshot", {}).get("changed")))
        result = {
            "status": "ok",
            "initialized": initialized,
            "dirty_scopes": sum(work_before.values()),
            "processed_turns": processed_turns,
            "updated_claims": updated_claims,
            "updated_sessions": updated_sessions,
            "new_snapshots": new_snapshots,
            "spool": spool,
            "recovered_review_jobs": recovered_review,
            "recovered_projections": recovered_projections,
            "review": review,
            "session_cards": cards,
            "refreshed_session_cards": refreshed_cards,
            "projections": projections,
            "hot_memory": hot,
            "snapshot_gc": snapshot_gc,
            "health": health,
        }
        _record_heartbeat_state(config, status="ok", work=work_before, processed_turns=processed_turns, updated_claims=updated_claims, updated_sessions=updated_sessions, new_snapshots=new_snapshots)
        return result
    except Exception as exc:
        _record_maintenance(config, error=str(exc))
        _record_heartbeat_state(config, status="failed", work=work_before, error=str(exc))
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
