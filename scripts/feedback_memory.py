#!/usr/bin/env python3
"""Explicit feedback updates utility, never mere retrieval frequency."""
from __future__ import annotations

import argparse
import json

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root
from proposal_manager import stage_memory_proposal


FEEDBACK_WEIGHT = {"used": 0.02, "helpful": 0.05, "user_confirmed": 0.10, "unhelpful": -0.08, "outdated": -0.12, "incorrect": -0.20, "irrelevant": -0.04}


def record_feedback(root, *, claim_id: str, feedback_type: str, source: str = "user", note: str = "", retrieval_uid: str = "") -> dict[str, object]:
    if feedback_type not in FEEDBACK_WEIGHT:
        raise ValueError(f"Unsupported feedback type: {feedback_type}")
    conn = open_db(root)
    claim = conn.execute("SELECT id, subject_id, memory_kind, topic, content, confidence, sensitivity FROM claims WHERE id=?", (claim_id,)).fetchone()
    if not claim:
        conn.close(); raise ValueError("Claim not found")
    weight = FEEDBACK_WEIGHT[feedback_type]
    conn.execute("INSERT INTO memory_feedback(claim_uid, retrieval_uid, feedback_type, source, weight, note) VALUES(?, ?, ?, ?, ?, ?)", (claim_id, retrieval_uid or None, feedback_type, source, weight, note))
    conn.execute("UPDATE claims SET confirmed_utility=MAX(-1.0, MIN(1.0, confirmed_utility + ?)), updated_at=CURRENT_TIMESTAMP WHERE id=?", (weight, claim_id))
    proposal_id = ""
    if feedback_type in {"incorrect", "outdated"}:
        action = "CORRECT" if feedback_type == "incorrect" else "SUPERSEDE"
        plan = {"plan_id": f"feedback:{claim_id}:{feedback_type}", "action": action, "subject_id": str(claim[1]), "target_claim_id": claim_id, "source_event_ids": [], "memory_kind": str(claim[2]), "topic": str(claim[3]), "content": str(claim[4]), "confidence": float(claim[5]), "sensitivity": str(claim[6]), "requires_review": True, "risk_reason": f"feedback:{feedback_type}"}
        proposal_id = stage_memory_proposal(conn, plan, origin="feedback", reason=f"feedback_{feedback_type}")
    conn.commit(); conn.close()
    return {"status": "ok", "claim_id": claim_id, "feedback_type": feedback_type, "weight": weight, "proposal_id": proposal_id}


def main() -> None:
    parser = argparse.ArgumentParser(description="Record explicit memory helpfulness feedback.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP); parser.add_argument("--claim-id", required=True); parser.add_argument("--type", required=True, choices=sorted(FEEDBACK_WEIGHT)); parser.add_argument("--source", default="user"); parser.add_argument("--note", default=""); parser.add_argument("--retrieval-id", default="")
    args = parser.parse_args(); emit(record_feedback(store_root(args.store), claim_id=args.claim_id, feedback_type=args.type, source=args.source, note=args.note, retrieval_uid=args.retrieval_id))


if __name__ == "__main__": main()
