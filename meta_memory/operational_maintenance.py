"""Bound derived operational data without touching memory evidence."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .legacy import bootstrap


def _days(config: AppConfig, name: str, fallback: int) -> int:
    try:
        return max(1, int(getattr(config, name, fallback)))
    except (TypeError, ValueError):
        return fallback


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()


def _is_due(value: str, *, hours: int = 24) -> bool:
    if not value:
        return True
    try:
        then = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - then.astimezone(timezone.utc) >= timedelta(hours=hours)
    except (TypeError, ValueError):
        return True


def _safe_report_path(root: Path, value: object) -> Path | None:
    """Only remove generated reports physically rooted in this store."""

    try:
        candidate = Path(str(value or "")).expanduser().resolve()
        dream_root = (root / "dream").resolve()
        if candidate.is_relative_to(dream_root):
            return candidate
    except (OSError, ValueError):
        return None
    return None


def cleanup_operational_data(config: AppConfig, *, force: bool = False) -> dict[str, Any]:
    """Prune only renewable telemetry/derived reports and optionally compact.

    Claims, source events, session evidence and unresolved jobs are never
    removed here.  The task is intentionally run at most daily from the short
    heartbeat, so it cannot become the dominant cost of normal user turns.
    """

    bootstrap()
    from _common import open_db, utc_now

    root = Path(config.store)
    operational_days = _days(config, "retention_operational_days", 90)
    retrieval_days = _days(config, "retention_retrieval_days", 30)
    dream_days = _days(config, "retention_dream_report_days", 30)
    conn = open_db(root)
    try:
        previous = conn.execute(
            "SELECT last_cleanup_at FROM operational_maintenance_state WHERE profile_id=?",
            (config.profile_id,),
        ).fetchone()
        if not force and previous and not _is_due(str(previous[0] or "")):
            return {
                "status": "skipped",
                "reason": "not_due",
                "last_cleanup_at": str(previous[0] or ""),
            }

        result: dict[str, int] = {}
        conn.execute("BEGIN IMMEDIATE")
        result["completed_projections"] = int(
            conn.execute(
                "DELETE FROM projection_outbox WHERE status='completed' AND completed_at<?",
                (_cutoff(operational_days),),
            ).rowcount
        )
        result["completed_review_jobs"] = int(
            conn.execute(
                "DELETE FROM review_jobs WHERE status IN ('applied','staged','planned') AND completed_at<?",
                (_cutoff(operational_days),),
            ).rowcount
        )
        result["retrieval_events"] = int(
            conn.execute("DELETE FROM retrieval_events WHERE created_at<?", (_cutoff(retrieval_days),)).rowcount
        )
        result["retrieval_log"] = int(
            conn.execute("DELETE FROM retrieval_log WHERE created_at<?", (_cutoff(retrieval_days),)).rowcount
        )

        # Keep the database relation and filesystem report in step.  A report
        # path outside the local dream directory is deliberately left alone.
        report_rows = conn.execute(
            """
            SELECT p.run_uid,p.workspace_id,p.subject_id,p.report_path
            FROM dream_run_reports AS p
            JOIN dream_runs AS r ON r.run_uid=p.run_uid
            WHERE p.profile_id=? AND r.status IN ('completed','idle') AND p.created_at<?
            """,
            (config.profile_id, _cutoff(dream_days)),
        ).fetchall()
        removed_reports = 0
        for run_uid, workspace_id, subject_id, report_path in report_rows:
            candidate = _safe_report_path(root, report_path)
            if candidate:
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    # Preserve the database row when a local lock prevents
                    # deletion; the next daily cleanup will retry it.
                    continue
            conn.execute(
                "DELETE FROM dream_run_reports WHERE run_uid=? AND workspace_id=? AND subject_id=?",
                (run_uid, workspace_id, subject_id),
            )
            removed_reports += 1
        result["dream_reports"] = removed_reports
        result["dream_runs"] = int(
            conn.execute(
                """
                DELETE FROM dream_runs
                WHERE profile_id=? AND status IN ('completed','idle') AND completed_at<?
                  AND NOT EXISTS (SELECT 1 FROM dream_run_reports AS p WHERE p.run_uid=dream_runs.run_uid)
                """,
                (config.profile_id, _cutoff(dream_days)),
            ).rowcount
        )
        now = utc_now()
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
        conn.execute(
            """
            INSERT INTO operational_maintenance_state(profile_id,last_cleanup_at,last_cleanup_json,updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(profile_id) DO UPDATE SET
                last_cleanup_at=excluded.last_cleanup_at,
                last_cleanup_json=excluded.last_cleanup_json,
                updated_at=excluded.updated_at
            """,
            (config.profile_id, now, payload, now),
        )
        conn.commit()
    finally:
        conn.close()

    # Hosted resumable uploads keep a tiny completion receipt so an ambiguous
    # final HTTP acknowledgement can be retried idempotently.  Remove only
    # upload directories that have been untouched beyond the operational
    # retention window; authoritative completed assets live elsewhere.
    upload_root = (root / "assets" / "uploads").resolve()
    removed_uploads = 0
    upload_cutoff = datetime.now(timezone.utc) - timedelta(days=operational_days)
    if upload_root.is_dir():
        for directory in upload_root.iterdir():
            try:
                resolved = directory.resolve()
                modified = datetime.fromtimestamp(directory.stat().st_mtime, timezone.utc)
                if directory.is_dir() and resolved.is_relative_to(upload_root) and modified < upload_cutoff:
                    shutil.rmtree(resolved)
                    removed_uploads += 1
            except OSError:
                continue
    result["asset_upload_receipts"] = removed_uploads

    # These operations are intentionally outside the write transaction.
    conn = open_db(root)
    compacted = False
    compact_error = ""
    try:
        try:
            conn.execute("PRAGMA optimize")
            if bool(getattr(config, "maintenance_compact_enabled", False)):
                conn.execute("VACUUM")
                compacted = True
        except Exception as exc:
            # A concurrent reader can temporarily prevent VACUUM.  Retention
            # has already committed; surface this as observability rather than
            # failing the whole heartbeat and retry on the next daily run.
            compact_error = str(exc)[:1000]
        if compacted:
            conn.execute(
                "UPDATE operational_maintenance_state SET last_compact_at=?,updated_at=? WHERE profile_id=?",
                (utc_now(), utc_now(), config.profile_id),
            )
        conn.commit()
    finally:
        conn.close()
    return {
        "status": "ok",
        "retention_days": {
            "operational": operational_days,
            "retrieval": retrieval_days,
            "dream_reports": dream_days,
        },
        "deleted": result,
        "compacted": compacted,
        "compact_error": compact_error,
    }


def operational_status(config: AppConfig) -> dict[str, Any]:
    """Small status surface safe for overview/status on every invocation."""

    bootstrap()
    from _common import open_db

    conn = open_db(Path(config.store))
    try:
        row = conn.execute(
            "SELECT last_cleanup_at,last_compact_at,last_cleanup_json FROM operational_maintenance_state WHERE profile_id=?",
            (config.profile_id,),
        ).fetchone()
        counts = {
            "completed_projections": int(
                conn.execute("SELECT COUNT(*) FROM projection_outbox WHERE status='completed'").fetchone()[0]
            ),
            "retrieval_events": int(conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0]),
            "dream_reports": int(
                conn.execute("SELECT COUNT(*) FROM dream_run_reports WHERE profile_id=?", (config.profile_id,)).fetchone()[0]
            ),
        }
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    finally:
        conn.close()
    try:
        last_deleted = json.loads(str(row[2] or "{}")) if row else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        last_deleted = {}
    return {
        "retention_days": {
            "operational": _days(config, "retention_operational_days", 90),
            "retrieval": _days(config, "retention_retrieval_days", 30),
            "dream_reports": _days(config, "retention_dream_report_days", 30),
        },
        "last_cleanup_at": str(row[0] or "") if row else "",
        "last_compact_at": str(row[1] or "") if row else "",
        "last_deleted": last_deleted,
        "counts": counts,
        "database": {
            "page_count": page_count,
            "freelist_pages": freelist,
            "approx_bytes": page_count * page_size,
        },
    }
