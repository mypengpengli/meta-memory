#!/usr/bin/env python3
"""Extract atomic, validated claims from changed session cards."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from build_session_card import QUESTION, sentence
from classify_memory import classify, first_sentence
from _common import DEFAULT_STORE_HELP, emit, open_db, sha256_text, store_root
from validate_memory_units import validate_unit
from llm_client import complete


SENSITIVE = re.compile(r"\b(health|medical|diagnosis|finance|salary|bank|relationship|divorce|password|phone|address)\b|健康|医疗|诊断|财务|工资|银行|关系|离婚|密码|电话|住址")
UNCERTAIN = re.compile(r"\b(maybe|perhaps|might|guess|probably|unsure)\b|可能|也许|大概|猜测|不确定|好像")
ACKNOWLEDGEMENT = re.compile(r"^(?:ok|okay|thanks|thank you|got it|continue|好的|谢谢|收到|继续)[!！。.]?$", re.I)
BOUNDARY = re.compile(r"(?<=[。！？!?.])\s*|\n+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract validated atomic memory units from changed session cards.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--subject-id")
    parser.add_argument("--card-id", type=int)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--include-assistant", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def is_question(content: str) -> bool:
    return bool(QUESTION.search(content.strip()))


def sensitivity(content: str) -> str:
    return "sensitive" if SENSITIVE.search(content) else "normal"


def atomic_clauses(content: str) -> list[str]:
    """Keep timeline facts, preferences, and procedures as separate units."""
    flat = " ".join(content.split())
    clauses: list[str] = []
    for sentence_text in BOUNDARY.split(flat):
        sentence_text = sentence_text.strip(" -;；")
        if not sentence_text:
            continue
        clauses.extend(part.strip() for part in re.split(r"\s+(?:and|but)\s+(?=(?:I|we|the |this |now |previously ))", sentence_text, flags=re.I) if part.strip())
    return list(dict.fromkeys(clauses))[:12]


def structured_fields(text: str, topic_hint: str, domain_hint: str) -> dict[str, object]:
    predicate, kind, subject, durability = "states", "domain", "user", 0.55
    if re.search(r"\b(?:prefer|preference|like responses)\b|偏好|喜欢.*(?:回答|方式)|希望.*(?:回答|说明)", text, re.I):
        predicate, kind, subject, durability = "prefers", "profile", "user", 0.9
    elif re.search(r"\b(?:now|currently|migrated|changed to|uses?)\b|现在|目前|已经改成|迁移到", text, re.I):
        predicate, kind, subject, durability = "current_state", "state", "project", 0.75
    elif re.search(r"\b(?:previously|used to|before)\b|以前|之前|曾经", text, re.I):
        predicate, kind, subject, durability = "historical_state", "event", "project", 0.7
    elif re.search(r"\b(?:please|when troubleshooting|when debugging|when .*?(?:debug|troubleshoot)|first .*? then)\b|以后.*(?:请|先)|排查.*(?:先|再)", text, re.I):
        predicate, kind, subject, durability = "procedure", "domain", "workflow", 0.8
    elif re.search(r"\b(?:goal|plan|will|need to)\b|目标|计划|需要", text, re.I):
        predicate, kind, subject, durability = "goal", "goal", "project", 0.65
    object_match = re.search(r"(?:uses?|use|to|为|是|改成|迁移到)\s+([A-Za-z0-9_.+/#-]{2,}|[\u4e00-\u9fff]{2,})", text, re.I)
    object_text = object_match.group(1) if object_match else text[:240]
    topic = topic_hint or re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", object_text.casefold()).strip("-") or "memory"
    return {"predicate": predicate, "unit_kind": kind, "subject_text": subject, "object_text": object_text[:240], "topic": topic[:80], "domain": domain_hint or "general", "durability": durability}


def optional_llm_units(content: str, raw_event_id: int) -> list[dict[str, object]]:
    """Use a configured structured LLM only for multi-fact/conditional text."""
    if not re.search(r"[。！？!?].+[。！？!?]|\b(?:but|however|previously|now|if)\b|以前|现在|如果|但是", content, re.I):
        return []
    prompt = (Path(__file__).resolve().parent.parent / "prompts" / "extract_memory_units.md").read_text(encoding="utf-8")
    try:
        response = complete(prompt, {"raw_event_id": raw_event_id, "content": content}) or {}
    except Exception:
        return []
    values = response.get("units") if isinstance(response, dict) else None
    if not isinstance(values, list):
        return []
    result: list[dict[str, object]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        ids = value.get("source_event_ids")
        if ids != [raw_event_id]:
            continue
        if not str(value.get("claim_text") or "").strip() or not str(value.get("predicate") or "").strip():
            continue
        result.append(value)
    return result[:12]


def extract_units(root, *, subject_id: str | None = None, card_id: int | None = None, limit: int = 20, include_assistant: bool = False, dry_run: bool = False) -> dict[str, object]:
    conn = open_db(root)
    clauses, params = ["needs_extraction=1"], []
    if subject_id:
        clauses.append("subject_id=?")
        params.append(subject_id)
    if card_id is not None:
        clauses.append("id=?")
        params.append(card_id)
    cards = conn.execute(f"SELECT id, subject_id, subject_name, session_id, source_event_ids FROM session_cards WHERE {' AND '.join(clauses)} ORDER BY updated_at LIMIT ?", (*params, max(1, limit))).fetchall()
    created: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for card_id_value, sid, name, session_id, source_ids_json in cards:
        ids = [int(value) for value in json.loads(source_ids_json or "[]")]
        if not ids:
            continue
        placeholders = ", ".join("?" for _ in ids)
        events = conn.execute(f"SELECT id, source_type, content, topic_hint, domain_hint FROM raw_events WHERE id IN ({placeholders}) ORDER BY id", ids).fetchall()
        for event_id, source_type, raw_content, topic_hint, domain_hint in events:
            source_type = str(source_type or "")
            content = " ".join(str(raw_content or "").split())[:2000]
            if not content or is_question(content) or ACKNOWLEDGEMENT.match(content):
                skipped.append({"raw_event_id": event_id, "reason": "question_or_empty"})
                continue
            if source_type == "conversation-assistant" and not include_assistant:
                skipped.append({"raw_event_id": event_id, "reason": "assistant_content_disabled"})
                continue
            llm_units = optional_llm_units(content, int(event_id))
            candidates = llm_units or [{"claim_text": clause} for clause in atomic_clauses(content)]
            for extracted in candidates:
                clause = str(extracted.get("claim_text") or "").strip()
                if is_question(clause) or ACKNOWLEDGEMENT.match(clause):
                    skipped.append({"raw_event_id": event_id, "reason": "non_memory_clause"})
                    continue
                fields = structured_fields(clause, str(topic_hint or ""), str(domain_hint or ""))
                if llm_units:
                    fields.update({"predicate": str(extracted.get("predicate") or fields["predicate"]), "unit_kind": str(extracted.get("memory_kind") or fields["unit_kind"]), "subject_text": str(extracted.get("subject_text") or fields["subject_text"]), "object_text": str(extracted.get("object_text") or fields["object_text"]), "topic": str(extracted.get("topic") or fields["topic"]), "domain": str(extracted.get("domain") or fields["domain"]), "durability": float(extracted.get("durability", fields["durability"]) or fields["durability"])})
                title = str(topic_hint or first_sentence(clause)[:80] or f"raw-event-{event_id}")
                classified = classify(title, clause, str(sid), str(name or "Unknown"))
                confidence = min(float(classified["suggested_payload"]["confidence"]), 0.25) if source_type == "conversation-assistant" else float(classified["suggested_payload"]["confidence"])
                confidence = float(extracted.get("confidence", confidence) or confidence) if llm_units else confidence
                unit = {"subject_id": str(sid), "claim_text": clause, "source_event_ids": [int(event_id)], "memory_kind": str(fields["unit_kind"]), "predicate": str(fields["predicate"]), "confidence": confidence, "uncertainty": float(extracted.get("uncertainty", 0.7 if UNCERTAIN.search(clause) else max(0.05, 1.0 - float(classified["classification_confidence"]))) or 0.0), "importance": float(extracted.get("importance", classified["suggested_payload"]["importance"]) or 0.0), "durability": float(fields["durability"])}
                validation = validate_unit(root, unit)
                if not validation["valid"]:
                    skipped.append({"raw_event_id": event_id, "reason": "validation", "errors": validation["errors"]})
                    continue
                digest = sha256_text(clause)
                key = sha256_text(f"{sid}:{event_id}:{fields['predicate']}:{digest}")
                observed_at = datetime.now(timezone.utc).isoformat()
                if dry_run:
                    created.append({"unit_id": None, "raw_event_id": event_id, "topic": fields["topic"], "predicate": fields["predicate"], "confidence": confidence})
                    continue
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO memory_units(unit_key, subject_id, subject_name, session_id, session_card_id, raw_event_id, source_event_ids, unit_kind, topic, content, content_hash, confidence, uncertainty, importance, sensitivity, source_type, status, domain, predicate, subject_text, object_text, qualifiers_json, valid_from, valid_to, observed_at, durability, entities_json, security_state, security_findings_json)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, '{}', ?, '', ?, ?, '[]', ?, ?)""",
                    (key, sid, name, session_id, card_id_value, event_id, json.dumps([event_id]), fields["unit_kind"], fields["topic"], clause, digest, confidence, unit["uncertainty"], unit["importance"], sensitivity(clause), source_type, fields["domain"], fields["predicate"], fields["subject_text"], fields["object_text"], observed_at, observed_at, fields["durability"], validation["security_state"], json.dumps(validation["security_findings"], ensure_ascii=False)),
                )
                if cursor.rowcount:
                    created.append({"unit_id": int(cursor.lastrowid), "raw_event_id": event_id, "topic": fields["topic"], "predicate": fields["predicate"], "confidence": confidence})
        if not dry_run:
            conn.execute("UPDATE session_cards SET needs_extraction=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (card_id_value,))
    if not dry_run:
        conn.commit()
    conn.close()
    return {"status": "ok", "dry_run": dry_run, "created": created, "skipped": skipped, "card_count": len(cards)}


def main() -> None:
    args = parse_args()
    emit(extract_units(store_root(args.store), subject_id=args.subject_id, card_id=args.card_id, limit=args.limit, include_assistant=args.include_assistant, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
