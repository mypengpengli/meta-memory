#!/usr/bin/env python3
"""Create a deterministic, reviewable consolidation plan from pending units."""
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Propose CREATE/CORROBORATE memory actions; it never writes memory directly.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--policy", choices=["conservative", "balanced", "aggressive"], default="conservative")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--out-file", help="Write the full JSON plan to this UTF-8 path")
    return parser.parse_args()


def choose_kind(unit_kind: str, confidence: float, sensitivity: str, policy: str) -> tuple[str, str]:
    if policy == "conservative":
        return "candidate", "unverified"
    if sensitivity == "sensitive":
        return "candidate", "unverified"
    if policy == "balanced" and confidence >= 0.85:
        return unit_kind, "verified"
    if policy == "aggressive" and confidence >= 0.7:
        return unit_kind, "verified"
    return "candidate", "unverified"


def build_plan(root, subject_id: str, *, policy: str = "conservative", limit: int = 20) -> dict[str, object]:
    conn = open_db(root)
    units = conn.execute(
        """
        SELECT id, unit_kind, topic, content, content_hash, confidence, uncertainty, importance, sensitivity,
               source_event_ids, raw_event_id
        FROM memory_units WHERE subject_id=? AND status='pending' ORDER BY id LIMIT ?
        """,
        (subject_id, max(1, limit)),
    ).fetchall()
    actions: list[dict[str, object]] = []
    for row in units:
        unit_id, unit_kind, topic, content, content_hash, confidence, uncertainty, importance, sensitivity, source_ids, raw_event_id = row
        existing = conn.execute("SELECT id, title, memory_kind, confidence FROM claims WHERE subject_id=? AND content_hash=? AND status NOT IN ('superseded', 'corrected')", (subject_id, content_hash)).fetchone()
        ids = json.loads(source_ids or "[]")
        base = {"plan_id": str(uuid.uuid4()), "subject_id": subject_id, "unit_id": int(unit_id), "source_event_ids": ids or [int(raw_event_id)], "topic": str(topic or "memory"), "confidence": round(float(confidence or 0.3), 3), "uncertainty": round(float(uncertainty or 0.7), 3), "importance": round(float(importance or 0.3), 3), "sensitivity": str(sensitivity or "normal")}
        if existing:
            actions.append({**base, "action": "CORROBORATE", "target_claim_id": str(existing[0]), "memory_kind": str(existing[2]), "title": str(existing[1]), "content": ""})
            continue
        kind, verification = choose_kind(str(unit_kind or "candidate"), float(confidence or 0.3), str(sensitivity or "normal"), policy)
        actions.append({**base, "action": "CREATE", "memory_kind": kind, "verification_state": verification, "title": str(topic or "Memory unit"), "content": str(content), "valid_from": "", "valid_to": ""})
    conn.close()
    return {"schema_version": 2, "subject_id": subject_id, "policy": policy, "actions": actions}


def main() -> None:
    args = parse_args()
    plan = build_plan(store_root(args.store), args.subject_id, policy=args.policy, limit=args.limit)
    if args.out_file:
        Path(args.out_file).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(plan)


if __name__ == "__main__":
    main()
