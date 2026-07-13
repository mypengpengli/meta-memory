"""Keep reusable methods out of factual claims and behind skill approval."""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from _common import open_db
from proposal_manager import get_proposal, reject_proposal, stage_skill_proposal


SKILL_ACTIONS = {"CREATE_SKILL", "PATCH_SKILL", "ADD_REFERENCE", "ADD_TEMPLATE", "ADD_SCRIPT", "ARCHIVE_SKILL", "IGNORE"}


def retrieve_procedures(root, *, subject_id: str, query: str, limit: int = 4) -> list[dict[str, object]]:
    """Return approved or candidate procedures as data, never as executable instructions."""
    terms = [term.casefold() for term in re.findall(r"[a-z0-9][\w.-]{1,}", query.casefold())][:12]
    conn = open_db(root)
    rows = conn.execute(
        "SELECT learning_uid, task_class, instruction_text, trigger_text, pitfall_text, confidence, status FROM procedural_learnings WHERE subject_id=? AND status IN ('candidate','approved') ORDER BY confidence DESC, created_at DESC LIMIT ?",
        (subject_id, max(limit * 4, limit)),
    ).fetchall()
    conn.close()
    scored = []
    for row in rows:
        text = " ".join(str(value or "") for value in row[1:5]).casefold()
        score = sum(term in text for term in terms)
        if score:
            scored.append({"learning_uid": str(row[0]), "task_class": str(row[1]), "instruction_text": str(row[2]), "trigger_text": str(row[3] or ""), "pitfall_text": str(row[4] or ""), "confidence": float(row[5] or 0), "status": str(row[6]), "score": score})
    return sorted(scored, key=lambda item: (item["score"], item["confidence"]), reverse=True)[:limit]


def slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", (value or "").casefold()).strip("-")
    return text[:64] or "general-procedure"


def create_learning(root, *, subject_id: str, task_class: str, instruction_text: str, source_event_ids: list[int], domain: str = "general", trigger_text: str = "", pitfall_text: str = "", confidence: float = 0.5, target_skill: str = "") -> dict[str, object]:
    conn = open_db(root); uid = str(uuid.uuid4())
    conn.execute("INSERT INTO procedural_learnings(learning_uid, subject_id, domain, task_class, trigger_text, instruction_text, pitfall_text, source_event_ids, confidence, target_skill) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (uid, subject_id, domain, task_class, trigger_text, instruction_text, pitfall_text, json.dumps(source_event_ids), confidence, target_skill or slug(task_class)))
    conn.commit(); conn.close()
    return {"learning_id": uid, "status": "candidate"}


def propose_from_units(root, *, subject_id: str, limit: int = 20) -> list[str]:
    conn = open_db(root)
    rows = conn.execute("SELECT id, topic, domain, content, source_event_ids, confidence FROM memory_units WHERE subject_id=? AND predicate='procedure' AND status IN ('pending','consolidated') ORDER BY id LIMIT ?", (subject_id, limit)).fetchall()
    conn.close()
    proposal_ids: list[str] = []
    for _, topic, domain, content, raw_ids, confidence in rows:
        learning = create_learning(root, subject_id=subject_id, task_class=slug(str(topic)), instruction_text=str(content), source_event_ids=json.loads(str(raw_ids or "[]")), domain=str(domain or "general"), confidence=float(confidence or 0.5), target_skill=slug(str(topic)))
        plan = {"action": "PATCH_SKILL", "skill": slug(str(topic)), "section": "User-specific procedures", "change": str(content), "source_event_ids": json.loads(str(raw_ids or "[]")), "confidence": float(confidence or 0.5), "risk": "medium", "learning_id": learning["learning_id"]}
        proposal_ids.append(stage_skill_proposal(root, plan, subject_id=subject_id))
    return proposal_ids


def skill_diff(root, proposal_id: str) -> str:
    proposal = get_proposal(root, proposal_id, kind="skill")
    if not proposal: raise ValueError("Skill proposal not found")
    return str(proposal["diff"])


def approve_skill(root, proposal_id: str, *, reviewer: str = "user") -> dict[str, object]:
    proposal = get_proposal(root, proposal_id, kind="skill")
    if not proposal or proposal["status"] != "pending": return {"status": "not_pending"}
    plan = proposal["plan"]
    if str(plan.get("action")) not in SKILL_ACTIONS or str(plan.get("action")) == "IGNORE":
        reject_proposal(root, proposal_id, note="ignored", kind="skill"); return {"status": "ignored"}
    conn = open_db(root); conn.execute("UPDATE skill_proposals SET status='approved', reviewed_at=CURRENT_TIMESTAMP, reviewed_by=? WHERE proposal_uid=? AND status='pending'", (reviewer, proposal_id)); conn.commit(); conn.close()
    return {"status": "approved", "mode": "proposal_only", "source_event_ids": plan.get("source_event_ids", []), "external_apply_required": True}
