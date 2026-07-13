#!/usr/bin/env python3
"""Durable, incremental projections for claim changes.

Claim rows are authoritative.  This queue keeps expensive search/hot
projections outside the approval transaction and makes retries observable.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, sha256_text, store_root, utc_now
from build_hot_memory import build_hot_memory


def enqueue_projection(conn, *, entity_type: str, entity_id: str, operation: str, payload: dict[str, object]) -> None:
    digest = sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    conn.execute(
        """INSERT INTO projection_outbox(entity_type, entity_id, operation, payload_hash, status, attempts, last_error, completed_at)
           VALUES(?, ?, ?, ?, 'pending', 0, NULL, NULL)
           ON CONFLICT(entity_type, entity_id, operation, payload_hash)
           DO UPDATE SET status='pending', attempts=0, last_error=NULL, completed_at=NULL""",
        (entity_type, entity_id, operation, digest),
    )


def _reindex_path(root: Path, path: str) -> None:
    script = Path(__file__).resolve().parent / "reindex_memory.py"
    subprocess.run([sys.executable, str(script), "--store", str(root), "--path", path], check=True, capture_output=True, text=True)


def process_projection_outbox(root: Path, *, limit: int = 100) -> dict[str, object]:
    conn = open_db(root)
    rows = conn.execute(
        "SELECT id, entity_type, entity_id, operation FROM projection_outbox WHERE status='pending' ORDER BY created_at, id LIMIT ?",
        (max(1, limit),),
    ).fetchall()
    conn.close()
    processed: list[dict[str, object]] = []
    for item_id, entity_type, entity_id, operation in rows:
        try:
            conn = open_db(root)
            if entity_type == "claim" and operation == "reindex":
                row = conn.execute("SELECT memory_path FROM claims WHERE id=?", (entity_id,)).fetchone()
                conn.close()
                if row and str(row[0] or ""):
                    _reindex_path(root, str(row[0]))
            elif entity_type == "hot" and operation == "refresh":
                subject, profile, workspace = str(entity_id).split("\x1f", 2)
                conn.close()
                build_hot_memory(root, subject_id=subject, profile_id=profile, workspace_id=workspace)
            else:
                conn.close()
            conn = open_db(root)
            conn.execute("UPDATE projection_outbox SET status='completed', completed_at=?, last_error=NULL WHERE id=?", (utc_now(), item_id))
            conn.commit(); conn.close()
            processed.append({"id": int(item_id), "status": "completed"})
        except Exception as exc:
            conn = open_db(root)
            conn.execute("UPDATE projection_outbox SET status='pending', attempts=attempts+1, last_error=? WHERE id=?", (str(exc)[:1000], item_id))
            conn.commit(); conn.close()
            processed.append({"id": int(item_id), "status": "retrying", "error": str(exc)})
    return {"status": "ok", "processed": processed}


def main() -> None:
    parser = argparse.ArgumentParser(description="Process incremental claim projections.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    emit(process_projection_outbox(store_root(args.store), limit=args.limit))


if __name__ == "__main__":
    main()
