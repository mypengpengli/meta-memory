#!/usr/bin/env python3
"""Find duplicate claims and propose auditable CORROBORATE actions, never merge text blindly."""
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect duplicate claim content and build safe consolidation suggestions.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--subject-id")
    parser.add_argument("--plan-out")
    args = parser.parse_args()
    root = store_root(args.store)
    conn = open_db(root)
    clauses = ["status NOT IN ('superseded', 'corrected')"]
    params: list[object] = []
    if args.subject_id:
        clauses.append("subject_id=?")
        params.append(args.subject_id)
    groups = conn.execute(
        f"SELECT subject_id, content_hash, COUNT(*) FROM claims WHERE {' AND '.join(clauses)} GROUP BY subject_id, content_hash HAVING COUNT(*) > 1",
        tuple(params),
    ).fetchall()
    actions: list[dict[str, object]] = []
    duplicates: list[dict[str, object]] = []
    for subject_id, content_hash, _count in groups:
        claims = conn.execute("SELECT id, title, memory_kind, confidence, importance FROM claims WHERE subject_id=? AND content_hash=? AND status NOT IN ('superseded','corrected') ORDER BY confidence DESC, created_at ASC", (subject_id, content_hash)).fetchall()
        keeper = claims[0]
        extras = []
        for claim_id, title, kind, confidence, importance in claims[1:]:
            sources = [int(row[0]) for row in conn.execute("SELECT raw_event_id FROM claim_sources WHERE claim_id=?", (claim_id,))]
            extras.append(str(claim_id))
            if sources:
                actions.append({"plan_id": str(uuid.uuid4()), "action": "CORROBORATE", "subject_id": subject_id, "target_claim_id": keeper[0], "source_event_ids": sources, "memory_kind": keeper[2], "topic": "duplicate", "title": keeper[1], "content": "", "confidence": max(float(keeper[3] or 0), float(confidence or 0)), "importance": max(float(keeper[4] or 0), float(importance or 0)), "sensitivity": "normal"})
        duplicates.append({"subject_id": subject_id, "keeper": str(keeper[0]), "duplicates": extras})
    conn.close()
    plan = {"schema_version": 2, "policy": "conservative", "actions": actions}
    if args.plan_out:
        Path(args.plan_out).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    emit({"status": "ok", "duplicate_groups": duplicates, "plan": plan, "count": len(duplicates)})


if __name__ == "__main__":
    main()
