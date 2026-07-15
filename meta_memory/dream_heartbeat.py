"""Public Dream heartbeat/deep lifecycle without breaking legacy commands."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import AppConfig
from .legacy import bootstrap


def run_heartbeat(config: AppConfig) -> dict[str, Any]:
    if not bool(getattr(config, "dream_heartbeat_enabled", True)):
        return {"status": "disabled", "reason": "dream.heartbeat_enabled=false"}
    from .maintenance import maintain

    return maintain(
        config,
        max_jobs=int(getattr(config, "dream_heartbeat_max_jobs", 50)),
        max_scopes=int(getattr(config, "dream_heartbeat_max_scopes", 20)),
    )


def _record_deep(config: AppConfig, *, status: str, error: str = "") -> None:
    bootstrap()
    from _common import open_db, utc_now

    conn = open_db(Path(config.store))
    try:
        conn.execute(
            """
            INSERT INTO dream_runtime_state(profile_id,workspace_id,deep_last_started_at,deep_last_completed_at,deep_last_status,deep_last_error,updated_at)
            VALUES(?, '*', ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id,workspace_id) DO UPDATE SET
                deep_last_started_at=excluded.deep_last_started_at,
                deep_last_completed_at=excluded.deep_last_completed_at,
                deep_last_status=excluded.deep_last_status,
                deep_last_error=excluded.deep_last_error,
                updated_at=excluded.updated_at
            """,
            (config.profile_id, utc_now(), utc_now(), status, error[:2000] or None, utc_now()),
        )
        conn.commit()
    finally:
        conn.close()


def run_deep(config: AppConfig, *, scan_days: int | None = None) -> dict[str, Any]:
    if not bool(getattr(config, "dream_deep_enabled", getattr(config, "dream_enabled", True))):
        return {"status": "disabled", "reason": "dream.deep_enabled=false"}
    from .dream import run_dream

    try:
        result = run_dream(config, scan_days=scan_days or int(getattr(config, "dream_deep_scan_days", config.dream_scan_days)))
    except Exception as exc:
        _record_deep(config, status="failed", error=str(exc))
        raise
    _record_deep(config, status="ok")
    return result


def dream_status(config: AppConfig) -> dict[str, Any]:
    bootstrap()
    from _common import ensure_store_ready, open_db

    ensure_store_ready(Path(config.store))
    conn = open_db(Path(config.store))
    try:
        row = conn.execute(
            """
            SELECT heartbeat_last_started_at,heartbeat_last_completed_at,heartbeat_last_status,heartbeat_last_error,
                   heartbeat_last_dirty_scopes,heartbeat_last_processed_turns,heartbeat_last_updated_claims,
                   heartbeat_last_updated_sessions,heartbeat_last_new_snapshots,
                   deep_last_completed_at,deep_last_status,deep_last_error
            FROM dream_runtime_state WHERE profile_id=? AND workspace_id='*'
            """,
            (config.profile_id,),
        ).fetchone()
    finally:
        conn.close()
    values = list(row) if row else [None] * 12
    return {
        "status": "ok",
        "heartbeat": {
            "enabled": bool(getattr(config, "dream_heartbeat_enabled", True)),
            "interval_minutes": int(getattr(config, "dream_heartbeat_interval_minutes", 10)),
            "last_started_at": values[0], "last_completed_at": values[1], "last_status": values[2], "last_error": values[3],
            "last_dirty_scopes": int(values[4] or 0), "last_processed_turns": int(values[5] or 0),
            "last_updated_claims": int(values[6] or 0), "last_updated_sessions": int(values[7] or 0),
            "last_new_snapshots": int(values[8] or 0),
        },
        "deep": {
            "enabled": bool(getattr(config, "dream_deep_enabled", getattr(config, "dream_enabled", True))),
            "schedule": str(getattr(config, "dream_deep_schedule", config.dream_schedule)),
            "last_completed_at": values[9], "last_status": values[10], "last_error": values[11],
        },
    }
