"""Persistent, auditable approval queues for memory and skill changes."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from _common import open_db


HIGH_RISK_ACTIONS = {"CORRECT", "SUPERSEDE", "DELETE"}


def risk_level(action: dict[str, object]) -> str:
    name = str(action.get("action", "")).upper()
    if str(action.get("sensitivity", "normal")) == "sensitive" or name in HIGH_RISK_ACTIONS:
        return "high"
    if bool(action.get("requires_review")) or (name == "CREATE" and str(action.get("verification_state")) == "verified") or name == "REFINE":
        return "medium"
    return "low"


def proposal_summary(action: dict[str, object]) -> str:
    return f"{str(action.get('action', 'CREATE')).upper()} {action.get('memory_kind', 'memory')}:{action.get('topic', 'memory')}"


def stage_memory_proposal(conn, action: dict[str, object], *, origin: str = "background_review", reason: str = "approval_required") -> str:
    existing = conn.execute("SELECT proposal_uid FROM write_proposals WHERE plan_json=? AND status='pending'", (json.dumps(action, ensure_ascii=False, sort_keys=True),)).fetchone()
    if existing:
        return str(existing[0])
    uid = str(uuid.uuid4())
    plan_text = json.dumps(action, ensure_ascii=False, sort_keys=True)
    diff = "\n".join([f"action: {action.get('action')}", f"target_claim_id: {action.get('target_claim_id', '')}", f"content: {action.get('content', '')}", f"reason: {reason}"])
    conn.execute(
        """INSERT INTO write_proposals(proposal_uid, subject_id, session_id, origin, action, risk_level, plan_json, summary, diff_text)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uid, str(action.get("subject_id", "")), str(action.get("session_id") or ""), origin, str(action.get("action", "")), risk_level(action), plan_text, proposal_summary(action), diff),
    )
    return uid


def list_proposals(root, *, status: str = "pending", kind: str = "memory") -> list[dict[str, object]]:
    conn = open_db(root)
    table = "skill_proposals" if kind == "skill" else "write_proposals"
    rows = conn.execute(f"SELECT proposal_uid, subject_id, origin, action, summary, status, created_at FROM {table} WHERE status=? ORDER BY created_at", (status,)).fetchall()
    conn.close()
    return [{"id": str(row[0]), "subject_id": str(row[1]), "origin": str(row[2]), "action": str(row[3]), "summary": str(row[4]), "status": str(row[5]), "created_at": str(row[6])} for row in rows]


def get_proposal(root, proposal_id: str, *, kind: str = "memory") -> dict[str, object] | None:
    conn = open_db(root)
    table = "skill_proposals" if kind == "skill" else "write_proposals"
    row = conn.execute(f"SELECT proposal_uid, subject_id, origin, action, plan_json, summary, diff_text, status, created_at, reviewed_at, review_note FROM {table} WHERE proposal_uid=?", (proposal_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return {"id": str(row[0]), "subject_id": str(row[1]), "origin": str(row[2]), "action": str(row[3]), "plan": json.loads(str(row[4])), "summary": str(row[5]), "diff": str(row[6] or ""), "status": str(row[7]), "created_at": str(row[8]), "reviewed_at": str(row[9] or ""), "review_note": str(row[10] or "")}


def reject_proposal(root, proposal_id: str, *, note: str = "", kind: str = "memory") -> bool:
    conn = open_db(root)
    table = "skill_proposals" if kind == "skill" else "write_proposals"
    changed = conn.execute(f"UPDATE {table} SET status='rejected', reviewed_at=?, review_note=? WHERE proposal_uid=? AND status='pending'", (datetime.now(timezone.utc).isoformat(), note, proposal_id)).rowcount
    conn.commit()
    conn.close()
    return bool(changed)


def stage_skill_proposal(root, plan: dict[str, object], *, subject_id: str, origin: str = "background_review") -> str:
    conn = open_db(root)
    uid = str(uuid.uuid4())
    content = json.dumps(plan, ensure_ascii=False, sort_keys=True)
    conn.execute("INSERT INTO skill_proposals(proposal_uid, subject_id, origin, action, skill, section, plan_json, summary, diff_text) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)", (uid, subject_id, origin, str(plan.get("action", "PATCH_SKILL")), str(plan.get("skill") or ""), str(plan.get("section") or ""), content, f"{plan.get('action', 'PATCH_SKILL')} {plan.get('skill', '')}", str(plan.get("change") or "")))
    conn.commit()
    conn.close()
    return uid
