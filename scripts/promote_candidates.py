#!/usr/bin/env python3
"""Promote a candidate only through the validated REFINE path."""
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root
from apply_memory_plan import apply_plan


PROMOTABLE_KINDS = ["profile", "state", "event", "relationship", "goal", "domain"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote a candidate claim through validation, provenance checks, and atomic writing.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--candidate", required=True, help="Candidate Markdown path")
    parser.add_argument("--target-kind", required=True, choices=PROMOTABLE_KINDS)
    parser.add_argument("--confidence", type=float)
    parser.add_argument("--out-file")
    args = parser.parse_args()
    root = store_root(args.store)
    candidate = Path(args.candidate).expanduser()
    if not candidate.is_absolute():
        candidate = (root / candidate).resolve()
    conn = open_db(root)
    row = conn.execute("SELECT id, subject_id, topic, title, content, confidence, importance, sensitivity FROM claims WHERE memory_path=? AND memory_kind='candidate'", (str(candidate),)).fetchone()
    if row is None:
        raise SystemExit("Candidate claim not found. V2 promotion requires a claim-backed candidate file.")
    source_ids = [int(item[0]) for item in conn.execute("SELECT raw_event_id FROM claim_sources WHERE claim_id=?", (row[0],))]
    conn.close()
    confidence = args.confidence if args.confidence is not None else max(float(row[5] or 0), 0.9 if args.target_kind == "profile" else 0.8)
    action = {"plan_id": str(uuid.uuid4()), "action": "REFINE", "subject_id": row[1], "target_claim_id": row[0], "source_event_ids": source_ids, "memory_kind": args.target_kind, "topic": row[2], "title": row[3], "content": row[4], "confidence": confidence, "importance": float(row[6] or 0.5), "sensitivity": row[7] or "normal", "verification_state": "verified"}
    result = apply_plan(root, {"schema_version": 2, "subject_id": row[1], "policy": "balanced", "actions": [action]})
    if args.out_file:
        Path(args.out_file).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(result)


if __name__ == "__main__":
    main()
