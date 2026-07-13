#!/usr/bin/env python3
"""Run the full 2.1 maintenance sequence in dependency order."""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root, utc_now
from apply_memory_plan import apply_plan
from background_review import recover_stuck_jobs, run_pending
from build_hot_memory import build_hot_memory
from build_session_card import build_cards
from consolidate_memories import build_plan
from detect_conflicts import find_conflict_candidates
from doctor import doctor
from extract_memory_units import extract_units


def run_script(script: Path, root: Path) -> dict[str, object]:
    result = subprocess.run([sys.executable, str(script), "--store", str(root)], check=True, capture_output=True, text=True)
    return {"script": script.name, "stdout": result.stdout.strip()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run migrations, review recovery, extraction, approvals, indexing, hot-memory refresh, and health checks.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP); parser.add_argument("--policy", choices=["conservative", "balanced", "aggressive"], default="conservative"); parser.add_argument("--max-review-jobs", type=int, default=20); parser.add_argument("--shadow-high-risk", action="store_true")
    args = parser.parse_args(); root = store_root(args.store); steps: list[dict[str, object]] = []
    conn = open_db(root); migrations = [str(row[0]) for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]; conn.close(); steps.append({"step": "run_migrations", "versions": migrations})
    steps.append({"step": "recover_stuck_jobs", "recovered": recover_stuck_jobs(root)})
    conn = open_db(root); cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(); closed = conn.execute("UPDATE sessions SET status='ended', ended_at=COALESCE(ended_at, ?), last_active_at=last_active_at WHERE status='active' AND last_active_at<?", (utc_now(), cutoff)).rowcount; conn.commit(); conn.close(); steps.append({"step": "close_stale_sessions", "closed": int(closed)})
    cards = build_cards(root, force=True, max_events=500); steps.append({"step": "build_missing_session_cards", "result": cards})
    units = extract_units(root, limit=500); steps.append({"step": "extract_pending_memory_units", "result": units})
    conn = open_db(root); subjects = [str(row[0]) for row in conn.execute("SELECT DISTINCT subject_id FROM memory_units WHERE status='pending'")]; conn.close()
    plans = [build_plan(root, subject, policy=args.policy, limit=500) for subject in subjects]; steps.append({"step": "generate_shadow_plans", "plans": [{"subject_id": plan["subject_id"], "actions": len(plan["actions"])} for plan in plans]})
    applied = [apply_plan(root, plan, skip_index=True) for plan in plans] if not args.shadow_high_risk else [apply_plan(root, plan, skip_index=True) for plan in plans]
    steps.append({"step": "apply_auto_approved_plans", "results": applied})
    review = run_pending(root, max_jobs=args.max_review_jobs, policy=args.policy, apply_low_risk=not args.shadow_high_risk); steps.append({"step": "review_jobs", "result": review})
    base = Path(__file__).resolve().parent
    for name, step in [("reindex_memory.py", "incremental_reindex"), ("embedding_index.py", "update_embeddings")]:
        try: steps.append({"step": step, "result": run_script(base / name, root)})
        except subprocess.CalledProcessError as exc: steps.append({"step": step, "status": "degraded", "error": exc.stderr.strip()})
    conn = open_db(root); hot_subjects = [str(row[0]) for row in conn.execute("SELECT DISTINCT subject_id FROM claims")]; conn.close(); steps.append({"step": "rebuild_hot_memory", "results": [build_hot_memory(root, subject_id=subject) for subject in hot_subjects]})
    steps.append({"step": "scan_conflicts", "conflicts": find_conflict_candidates(root)})
    for name, step in [("score_memories.py", "compact_feedback_scores"), ("build_views.py", "build_views"), ("lint_memory.py", "lint")]:
        try: steps.append({"step": step, "result": run_script(base / name, root)})
        except subprocess.CalledProcessError as exc: steps.append({"step": step, "status": "degraded", "error": exc.stderr.strip()})
    steps.append({"step": "evaluation_smoke_test", "result": doctor(root)})
    emit({"status": "ok", "steps": steps})


if __name__ == "__main__": main()
