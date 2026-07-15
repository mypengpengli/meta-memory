#!/usr/bin/env python3
"""Leased, retryable projection worker for derived indexes and hot snapshots."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, sha256_text, store_root, utc_now
from build_hot_memory import build_hot_memory
from config import get


def enqueue_projection(conn, *, entity_type: str, entity_id: str, operation: str, payload: dict[str, object]) -> None:
    digest = sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    conn.execute(
        """INSERT INTO projection_outbox(entity_type, entity_id, operation, payload_hash, status, attempts, last_error, completed_at, lease_owner, leased_until, next_retry_at, dead_letter_at)
           VALUES(?, ?, ?, ?, 'pending', 0, NULL, NULL, NULL, NULL, NULL, NULL)
           ON CONFLICT(entity_type, entity_id, operation, payload_hash)
           DO UPDATE SET status='pending', attempts=0, last_error=NULL, completed_at=NULL, lease_owner=NULL, leased_until=NULL, next_retry_at=NULL, dead_letter_at=NULL""",
        (entity_type, entity_id, operation, digest),
    )


def _reindex_path(root: Path, path: str) -> None:
    """Call the projection service in-process; never spawn a user-turn subprocess."""
    from reindex_memory import main as reindex_cli

    previous = sys.argv
    try:
        sys.argv = ["reindex_memory.py", "--store", str(root), "--path", path]
        # The compatibility CLI emits JSON, while the worker records its own
        # result in the durable outbox.  Keep that implementation detail out
        # of worker output without losing exceptions.
        with contextlib.redirect_stdout(io.StringIO()):
            reindex_cli()
    finally:
        sys.argv = previous


def _claim_batch(root: Path, worker_id: str, *, limit: int, lease_seconds: int = 300) -> list[tuple[int, str, str, str]]:
    conn = open_db(root); now = utc_now(); until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
    conn.execute("BEGIN IMMEDIATE")
    rows = conn.execute(
        """SELECT id, entity_type, entity_id, operation FROM projection_outbox
           WHERE status='pending' AND (next_retry_at IS NULL OR next_retry_at<=?)
             AND (leased_until IS NULL OR leased_until<?)
           ORDER BY created_at, id LIMIT ?""",
        (now, now, max(1, limit)),
    ).fetchall()
    claimed: list[tuple[int, str, str, str]] = []
    for row in rows:
        changed = conn.execute(
            """UPDATE projection_outbox SET status='running', attempts=attempts+1, lease_owner=?, leased_until=?
               WHERE id=? AND status='pending' AND (leased_until IS NULL OR leased_until<?)""",
            (worker_id, until, row[0], now),
        ).rowcount
        if changed:
            claimed.append((int(row[0]), str(row[1]), str(row[2]), str(row[3])))
    conn.commit(); conn.close()
    return claimed


def renew_lease(root: Path, item_id: int, worker_id: str, *, lease_seconds: int = 300) -> bool:
    until = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat(); conn = open_db(root)
    changed = conn.execute("UPDATE projection_outbox SET leased_until=? WHERE id=? AND status='running' AND lease_owner=?", (until, item_id, worker_id)).rowcount
    conn.commit(); conn.close(); return bool(changed)


def recover_stuck_outbox(root: Path, *, timeout_seconds: int = 600) -> int:
    """Return expired running projections to the pending queue."""
    now = utc_now()
    conn = open_db(root)
    try:
        changed = conn.execute(
            """
            UPDATE projection_outbox
            SET status='pending',lease_owner=NULL,leased_until=NULL,next_retry_at=?,last_error=COALESCE(last_error, 'lease_recovered')
            WHERE status='running' AND (leased_until<? OR leased_until IS NULL)
            """,
            (now, now),
        ).rowcount
        conn.commit()
        return int(changed)
    finally:
        conn.close()


def _complete(root: Path, item_id: int, worker_id: str) -> None:
    conn = open_db(root)
    conn.execute("UPDATE projection_outbox SET status='completed', completed_at=?, last_error=NULL, lease_owner=NULL, leased_until=NULL WHERE id=? AND status='running' AND lease_owner=?", (utc_now(), item_id, worker_id))
    conn.commit(); conn.close()


def _retry(root: Path, item_id: int, worker_id: str, error: Exception) -> str:
    conn = open_db(root); row = conn.execute("SELECT attempts FROM projection_outbox WHERE id=?", (item_id,)).fetchone(); attempts = int(row[0] or 0) if row else 0
    limit = int(get("worker.retry_limit"))
    if attempts >= limit:
        status, retry_at, dead = "dead_letter", None, utc_now()
    else:
        status, retry_at, dead = "pending", (datetime.now(timezone.utc) + timedelta(seconds=min(3600, 15 * (2 ** max(0, attempts - 1))))).isoformat(), None
    conn.execute("UPDATE projection_outbox SET status=?, next_retry_at=?, dead_letter_at=?, last_error=?, lease_owner=NULL, leased_until=NULL WHERE id=? AND lease_owner=?", (status, retry_at, dead, str(error)[:1000], item_id, worker_id))
    conn.commit(); conn.close(); return status


def _refresh_hot_scope(root: Path, entity_id: str) -> dict[str, object]:
    """Build one canonical snapshot for a coalesced scope/generation.

    A claim write can enqueue many ``hot/refresh`` outbox rows in one review
    batch.  Hot memory is a scope projection, not a claim projection, so
    processing each row used to regenerate the same files repeatedly.  The
    compare-and-swap below also prevents maintenance from doing a second build
    once this worker has completed the exact dirty generation.
    """

    scope = entity_id.split("\x1f")
    if len(scope) < 3 or not all(scope[:3]):
        raise ValueError("invalid hot projection scope")
    subject, profile, workspace = scope[:3]
    agent = scope[3] if len(scope) > 3 else ""
    conn = open_db(root)
    try:
        row = conn.execute(
            """
            SELECT claim_generation FROM workspace_runtime_state
            WHERE profile_id=? AND workspace_id=? AND subject_id=? AND agent_id=?
            """,
            (profile, workspace, subject, agent),
        ).fetchone()
        canonical_agent = agent
        # Older producers encoded origin_agent_id in every hot outbox key,
        # while shared runtime state intentionally uses the empty agent scope.
        # Prefer that canonical shared scope when it exists.
        if row is None and agent:
            row = conn.execute(
                """
                SELECT claim_generation FROM workspace_runtime_state
                WHERE profile_id=? AND workspace_id=? AND subject_id=? AND agent_id=''
                """,
                (profile, workspace, subject),
            ).fetchone()
            if row is not None:
                canonical_agent = ""
        generation = int(row[0] or 0) if row else None
    finally:
        conn.close()
    snapshot = build_hot_memory(
        root,
        subject_id=subject,
        profile_id=profile,
        workspace_id=workspace,
        agent_id=canonical_agent,
        generation=generation,
    )
    if generation is not None:
        conn = open_db(root)
        try:
            conn.execute(
                """
                UPDATE workspace_runtime_state
                SET hot_dirty=CASE WHEN claim_generation=? THEN 0 ELSE 1 END,
                    hot_generation=CASE WHEN claim_generation=? THEN ? ELSE hot_generation END,
                    last_maintained_at=?,last_success_at=?,last_error=NULL,updated_at=?
                WHERE profile_id=? AND workspace_id=? AND subject_id=? AND agent_id=?
                """,
                (
                    generation,
                    generation,
                    generation,
                    utc_now(),
                    utc_now(),
                    utc_now(),
                    profile,
                    workspace,
                    subject,
                    canonical_agent,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return {"scope": {"subject_id": subject, "profile_id": profile, "workspace_id": workspace, "agent_id": canonical_agent}, "snapshot": snapshot, "generation": generation}


def process_projection_outbox(root: Path, *, limit: int = 100, worker_id: str = "") -> dict[str, object]:
    worker_id = worker_id or f"projection:{uuid.uuid4()}"
    rows = _claim_batch(root, worker_id, limit=limit)
    processed: list[dict[str, object]] = []
    hot_groups: dict[str, list[int]] = {}
    ordinary_rows: list[tuple[int, str, str, str]] = []
    for item in rows:
        item_id, entity_type, entity_id, operation = item
        if entity_type == "hot" and operation == "refresh":
            hot_groups.setdefault(entity_id, []).append(item_id)
        else:
            ordinary_rows.append(item)

    # Reindex claim projections first, then build each affected hot scope once
    # against the final generation for this worker batch.
    for item_id, entity_type, entity_id, operation in ordinary_rows:
        try:
            if entity_type == "claim" and operation == "reindex":
                conn = open_db(root); row = conn.execute("SELECT memory_path FROM claims WHERE id=?", (entity_id,)).fetchone(); conn.close()
                if row and str(row[0] or ""):
                    _reindex_path(root, str(row[0]))
            _complete(root, item_id, worker_id)
            processed.append({"id": item_id, "status": "completed"})
        except Exception as exc:
            processed.append({"id": item_id, "status": _retry(root, item_id, worker_id, exc), "error": str(exc)})
    for entity_id, item_ids in hot_groups.items():
        try:
            result = _refresh_hot_scope(root, entity_id)
            for index, item_id in enumerate(item_ids):
                _complete(root, item_id, worker_id)
                processed.append(
                    {
                        "id": item_id,
                        "status": "completed",
                        "coalesced": len(item_ids) > 1,
                        "scope_refresh": index == 0,
                        "hot": result,
                    }
                )
        except Exception as exc:
            for item_id in item_ids:
                processed.append({"id": item_id, "status": _retry(root, item_id, worker_id, exc), "error": str(exc)})
    return {"status": "ok", "worker_id": worker_id, "processed": processed, "hot_scopes_refreshed": len(hot_groups)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Process leased incremental claim projections.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP); parser.add_argument("--limit", type=int, default=100); parser.add_argument("--worker-id", default=""); parser.add_argument("--loop", action="store_true"); parser.add_argument("--poll-seconds", type=float, default=2)
    args = parser.parse_args(); root = store_root(args.store)
    if args.loop:
        while True:
            process_projection_outbox(root, limit=args.limit, worker_id=args.worker_id)
            time.sleep(max(.1, args.poll_seconds))
    else:
        emit(process_projection_outbox(root, limit=args.limit, worker_id=args.worker_id))


if __name__ == "__main__":
    main()
