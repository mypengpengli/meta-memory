#!/usr/bin/env python3
"""Lightweight heartbeat: group raw evidence into cards, never auto-edit long-term memory."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from build_session_card import build_cards
from _common import DEFAULT_STORE_HELP, emit, open_db, store_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the safe session-card heartbeat.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--subject-id")
    parser.add_argument("--interval-minutes", type=int, default=30)
    parser.add_argument("--min-pending", type=int, default=5)
    parser.add_argument("--max-events", type=int, default=100)
    parser.add_argument("--policy", choices=["conservative", "balanced", "aggressive"], default="conservative")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--extract-units", action="store_true", help="Run unit extraction after card creation; does not write claims")
    return parser.parse_args()


def parse_time(value: str | None):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace(" ", "T"))
    except ValueError:
        return None
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def main() -> None:
    args = parse_args()
    root = store_root(args.store)
    conn = open_db(root)
    clauses = ["r.processed_state='pending'"]
    params: list[object] = []
    if args.subject_id:
        clauses.append("r.subject_id=?")
        params.append(args.subject_id)
    rows = conn.execute(
        f"""SELECT r.subject_id, COUNT(*), c.last_heartbeat_at
            FROM raw_events r LEFT JOIN maintenance_cursor c ON c.subject_id=r.subject_id
            WHERE {' AND '.join(clauses)} GROUP BY r.subject_id ORDER BY MIN(r.id)""",
        tuple(params),
    ).fetchall()
    summaries: list[dict[str, object]] = []
    current = datetime.now(timezone.utc)
    for subject_id, pending_count, last_heartbeat in rows:
        elapsed = parse_time(last_heartbeat)
        flush = int(pending_count) >= args.min_pending or elapsed is None or current - elapsed >= timedelta(minutes=args.interval_minutes)
        if flush:
            result = build_cards(root, subject_id=str(subject_id), min_events=args.min_pending, max_events=args.max_events, force=True, dry_run=args.dry_run)
            summaries.append({"subject_id": subject_id, "pending_count": pending_count, "organize": True, "reason": "threshold_or_interval", "cards": result["cards"]})
        else:
            summaries.append({"subject_id": subject_id, "pending_count": pending_count, "organize": False, "reason": "waiting_for_threshold_or_interval"})
        if not args.dry_run:
            conn.execute("INSERT INTO maintenance_cursor(subject_id, last_heartbeat_at) VALUES(?, ?) ON CONFLICT(subject_id) DO UPDATE SET last_heartbeat_at=excluded.last_heartbeat_at", (subject_id, current.isoformat()))
    if args.subject_id and not rows and not args.dry_run:
        conn.execute("INSERT INTO maintenance_cursor(subject_id, last_heartbeat_at) VALUES(?, ?) ON CONFLICT(subject_id) DO UPDATE SET last_heartbeat_at=excluded.last_heartbeat_at", (args.subject_id, current.isoformat()))
    conn.commit()
    conn.close()
    units = None
    if args.extract_units and not args.dry_run:
        from extract_memory_units import extract_units

        units = extract_units(root, subject_id=args.subject_id, limit=args.max_events)
    emit({"status": "ok", "policy": args.policy, "dry_run": args.dry_run, "subjects": summaries or ([{"subject_id": args.subject_id, "pending_count": 0, "organize": False, "reason": "no_pending_events"}] if args.subject_id else []), "session_units": units, "indexed": False})


if __name__ == "__main__":
    main()
