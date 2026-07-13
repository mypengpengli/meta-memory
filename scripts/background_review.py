#!/usr/bin/env python3
"""Restricted background reviewer: it creates plans, never direct writes."""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timedelta, timezone

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root, utc_now
from apply_memory_plan import apply_plan
from build_session_card import build_cards
from consolidate_memories import build_plan
from extract_memory_units import extract_units


def enqueue_review(root, *, subject_id: str, session_id: str, event_start_id: int, event_end_id: int, trigger_type: str, workspace_id: str = "default") -> dict[str, object]:
    key = f"{subject_id}:{workspace_id}:{session_id}:{event_end_id}:{trigger_type}:v1"
    conn = open_db(root)
    existing = conn.execute("SELECT job_uid, status FROM review_jobs WHERE job_key=?", (key,)).fetchone()
    if existing:
        conn.close(); return {"job_id": str(existing[0]), "status": str(existing[1]), "deduplicated": True}
    uid = str(uuid.uuid4())
    conn.execute("INSERT INTO review_jobs(job_uid, job_key, subject_id, session_id, event_start_id, event_end_id, trigger_type) VALUES(?, ?, ?, ?, ?, ?, ?)", (uid, key, subject_id, session_id, event_start_id, event_end_id, trigger_type))
    conn.commit(); conn.close()
    return {"job_id": uid, "status": "pending", "deduplicated": False}


def recover_stuck_jobs(root, *, timeout_minutes: int = 10) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)).isoformat()
    conn = open_db(root)
    changed = conn.execute("UPDATE review_jobs SET status='pending', started_at=NULL, next_retry_at=? WHERE status='running' AND started_at<?", (utc_now(), cutoff)).rowcount
    conn.commit(); conn.close()
    return int(changed)


def _digest(conn, subject_id: str, session_id: str, limit: int = 20) -> str:
    rows = conn.execute("SELECT role, content FROM session_messages WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id or f"implicit:{subject_id}", limit)).fetchall()
    return "\n".join(f"{row[0]}: {str(row[1])[:500]}" for row in reversed(rows))[:12000]


def run_pending(root, *, max_jobs: int = 10, policy: str = "conservative", apply_low_risk: bool = False) -> dict[str, object]:
    conn = open_db(root)
    jobs = conn.execute("SELECT job_uid, subject_id, session_id FROM review_jobs WHERE status='pending' AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY created_at LIMIT ?", (utc_now(), max_jobs)).fetchall()
    conn.close()
    results: list[dict[str, object]] = []
    for uid, subject_id, session_id in jobs:
        conn = open_db(root)
        if not conn.execute("UPDATE review_jobs SET status='running', attempt_count=attempt_count+1, started_at=? WHERE job_uid=? AND status='pending'", (utc_now(), uid)).rowcount:
            conn.close(); continue
        digest = _digest(conn, str(subject_id), str(session_id)); conn.execute("UPDATE review_jobs SET input_digest=? WHERE job_uid=?", (digest, uid)); conn.commit(); conn.close()
        try:
            build_cards(root, subject_id=str(subject_id), session_id=str(session_id) if session_id else None, force=True)
            units = extract_units(root, subject_id=str(subject_id), limit=100)
            plan = build_plan(root, str(subject_id), policy=policy, limit=100)
            for action in plan["actions"]:
                action["origin"] = "background_review"
            outcome = apply_plan(root, plan, skip_index=True) if apply_low_risk else {"status": "shadow", "actions": plan["actions"]}
            status = "applied" if apply_low_risk and all(item.get("status") in {"applied", "review", "skipped"} for item in outcome.get("results", [])) else "staged" if plan["actions"] else "planned"
            conn = open_db(root); conn.execute("UPDATE review_jobs SET status=?, memory_plan_json=?, completed_at=?, last_error=NULL WHERE job_uid=?", (status, json.dumps(plan, ensure_ascii=False), utc_now(), uid)); conn.commit(); conn.close()
            results.append({"job_id": uid, "status": status, "units": units["created"], "action_count": len(plan["actions"]), "outcome": outcome})
        except Exception as exc:
            conn = open_db(root); conn.execute("UPDATE review_jobs SET status=CASE WHEN attempt_count>=5 THEN 'dead_letter' ELSE 'failed' END, last_error=?, completed_at=? WHERE job_uid=?", (str(exc), utc_now(), uid)); conn.commit(); conn.close(); results.append({"job_id": uid, "status": "failed", "error": str(exc)})
    return {"status": "ok", "results": results}


def main() -> None:
    parser = argparse.ArgumentParser(description="Queue or process restricted background memory reviews.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP); parser.add_argument("--subject-id"); parser.add_argument("--session-id", default=""); parser.add_argument("--event-start-id", type=int, default=0); parser.add_argument("--event-end-id", type=int, default=0); parser.add_argument("--trigger", default="scheduled_maintenance"); parser.add_argument("--run", action="store_true"); parser.add_argument("--max-jobs", type=int, default=10); parser.add_argument("--apply-low-risk", action="store_true")
    args = parser.parse_args(); root = store_root(args.store)
    if args.run: emit(run_pending(root, max_jobs=args.max_jobs, apply_low_risk=args.apply_low_risk))
    elif args.subject_id: emit(enqueue_review(root, subject_id=args.subject_id, session_id=args.session_id, event_start_id=args.event_start_id, event_end_id=args.event_end_id, trigger_type=args.trigger))
    else: raise SystemExit("Use --run or supply --subject-id to queue a review job.")


if __name__ == "__main__": main()
