#!/usr/bin/env python3
"""Recovery and health maintenance; consolidation belongs to the review worker."""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root, utc_now
from background_review import recover_stuck_jobs
from backfill_session_scopes import backfill
from build_hot_memory import garbage_collect_snapshots
from detect_conflicts import find_conflict_candidates
from doctor import doctor
from projection_outbox import process_projection_outbox


def run_script(script: Path, root: Path) -> dict[str, object]:
    result = subprocess.run([sys.executable, str(script), "--store", str(root)], check=True, capture_output=True, text=True)
    return {"script": script.name, "stdout": result.stdout.strip()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover leases, backfill scopes, compact projections, and report health. It never consolidates evidence directly.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP); parser.add_argument("--max-projection-jobs", type=int, default=500); parser.add_argument("--skip-projections", action="store_true")
    args = parser.parse_args(); root = store_root(args.store); steps: list[dict[str, object]] = []
    conn = open_db(root); migrations = [str(row[0]) for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]; conn.close(); steps.append({"step": "run_migrations", "versions": migrations})
    steps.append({"step": "backfill_session_scopes", "result": backfill(root)})
    steps.append({"step": "recover_stuck_review_jobs", "recovered": recover_stuck_jobs(root)})
    steps.append({"step": "garbage_collect_hot_snapshots", "result": garbage_collect_snapshots(root)})
    conn = open_db(root); cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(); closed = conn.execute("UPDATE sessions SET status='ended', ended_at=COALESCE(ended_at, ?), last_active_at=last_active_at WHERE status='active' AND last_active_at<?", (utc_now(), cutoff)).rowcount; conn.commit(); conn.close(); steps.append({"step": "close_stale_sessions", "closed": int(closed)})
    if not args.skip_projections:
        steps.append({"step": "process_projection_outbox", "result": process_projection_outbox(root, limit=max(1, args.max_projection_jobs))})
    steps.append({"step": "scan_conflicts", "conflicts": find_conflict_candidates(root)})
    base = Path(__file__).resolve().parent
    for name, step in [("score_memories.py", "compact_feedback_scores"), ("build_views.py", "build_views"), ("lint_memory.py", "lint")]:
        try: steps.append({"step": step, "result": run_script(base / name, root)})
        except subprocess.CalledProcessError as exc: steps.append({"step": step, "status": "degraded", "error": exc.stderr.strip()})
    steps.append({"step": "evaluation_smoke_test", "result": doctor(root)})
    emit({"status": "ok", "mode": "recovery_only", "steps": steps})


if __name__ == "__main__":
    main()
