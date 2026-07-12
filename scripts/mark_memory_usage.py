#!/usr/bin/env python3
"""Record explicit evidence that a retrieved memory was actually useful.

Retrieval alone never increases durable rank. Hosts call this only when an agent
used a memory in its response, a task succeeded because of it, or a user gave
positive feedback.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mark selected memories as actually used or confirmed.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--retrieval-event-id", type=int)
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--used", action="store_true", help="Mark a memory as used in the answer")
    parser.add_argument("--user-confirmed", action="store_true", help="Record direct user confirmation")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.retrieval_event_id and not args.path:
        raise SystemExit("Provide --retrieval-event-id or one or more --path values.")
    root = store_root(args.store)
    conn = open_db(root)
    paths = list(args.path)
    if args.retrieval_event_id:
        row = conn.execute("SELECT selected_paths FROM retrieval_events WHERE id=?", (args.retrieval_event_id,)).fetchone()
        if row is None:
            raise SystemExit("Retrieval event not found")
        paths.extend(json.loads(row[0] or "[]"))
    paths = list(dict.fromkeys(str(path) for path in paths if str(path).strip()))
    now = datetime.now(timezone.utc).isoformat()
    for path in paths:
        row = conn.execute("SELECT COALESCE(hit_count, 0), COALESCE(confidence, 0.0) FROM scores WHERE path=?", (path,)).fetchone()
        hit_count = int(row[0] if row else 0) + 1
        confidence = float(row[1] if row else 0.0)
        conn.execute(
            """INSERT INTO scores(path, hit_count, confidence, rank_score, last_hit_at)
               VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET hit_count=excluded.hit_count, rank_score=excluded.rank_score, last_hit_at=excluded.last_hit_at""",
            (path, hit_count, confidence, round(math.log1p(hit_count) + confidence, 4), now),
        )
    if args.retrieval_event_id:
        conn.execute("UPDATE retrieval_events SET used_node_ids=?, user_confirmed=?, feedback_at=? WHERE id=?", (json.dumps(paths, ensure_ascii=False) if args.used else "[]", 1 if args.user_confirmed else 0, now, args.retrieval_event_id))
    conn.commit()
    conn.close()
    emit({"status": "ok", "marked_paths": paths, "used": args.used, "user_confirmed": args.user_confirmed})


if __name__ == "__main__":
    main()
