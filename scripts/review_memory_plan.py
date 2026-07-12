#!/usr/bin/env python3
"""Inspect and explicitly approve queued high-risk memory-plan actions."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List or approve Meta Memory review-queue items.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--plan-id")
    parser.add_argument("--approve", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = store_root(args.store)
    conn = open_db(root)
    if not args.approve:
        rows = conn.execute("SELECT plan_id, subject_id, reason, payload, status, created_at FROM review_queue WHERE status='pending' ORDER BY id").fetchall()
        conn.close()
        emit({"status": "ok", "items": [{"plan_id": row[0], "subject_id": row[1], "reason": row[2], "payload": json.loads(row[3]), "status": row[4], "created_at": row[5]} for row in rows]})
        return
    if not args.plan_id:
        raise SystemExit("--plan-id is required with --approve")
    row = conn.execute("SELECT payload FROM review_queue WHERE plan_id=? AND status='pending'", (args.plan_id,)).fetchone()
    if row is None:
        raise SystemExit("Pending review item not found")
    action = json.loads(row[0])
    conn.close()
    from apply_memory_plan import apply_plan

    result = apply_plan(root, {"schema_version": 2, "subject_id": action["subject_id"], "policy": "balanced", "actions": [action]}, review_approved=True)
    if result["status"] == "ok":
        conn = open_db(root)
        conn.execute("UPDATE review_queue SET status='approved', reviewed_at=CURRENT_TIMESTAMP WHERE plan_id=?", (args.plan_id,))
        conn.commit()
        conn.close()
    emit(result)


if __name__ == "__main__":
    main()
