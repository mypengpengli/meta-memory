#!/usr/bin/env python3
"""Extract atomically-scoped candidate units from session cards, conservatively."""
from __future__ import annotations

import argparse
import json
import re

from build_session_card import QUESTION, sentence
from classify_memory import classify, first_sentence
from _common import DEFAULT_STORE_HELP, emit, open_db, sha256_text, store_root


SENSITIVE = re.compile(r"\b(health|medical|diagnosis|finance|salary|bank|relationship|divorce|password|phone|address)\b|健康|医疗|诊断|财务|工资|银行|关系|离婚|密码|电话|住址")
UNCERTAIN = re.compile(r"\b(maybe|perhaps|might|guess|probably|unsure)\b|可能|也许|大概|猜测|不确定|好像")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract one-fact memory units from changed session cards.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--subject-id")
    parser.add_argument("--card-id", type=int)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--include-assistant", action="store_true", help="Keep assistant content as low-confidence candidates; off by default")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def is_question(content: str) -> bool:
    return bool(QUESTION.search(content.strip()))


def sensitivity(content: str) -> str:
    return "sensitive" if SENSITIVE.search(content) else "normal"


def extract_units(root, *, subject_id: str | None = None, card_id: int | None = None, limit: int = 20, include_assistant: bool = False, dry_run: bool = False) -> dict[str, object]:
    conn = open_db(root)
    clauses = ["needs_extraction = 1"]
    params: list[object] = []
    if subject_id:
        clauses.append("subject_id = ?")
        params.append(subject_id)
    if card_id is not None:
        clauses.append("id = ?")
        params.append(card_id)
    cards = conn.execute(f"SELECT id, subject_id, subject_name, session_id, source_event_ids FROM session_cards WHERE {' AND '.join(clauses)} ORDER BY updated_at LIMIT ?", (*params, max(1, limit))).fetchall()
    created: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    for card in cards:
        cid, sid, name, session_id, source_ids_json = card
        source_ids = [int(value) for value in json.loads(source_ids_json or "[]")]
        if not source_ids:
            continue
        placeholders = ", ".join("?" for _ in source_ids)
        rows = conn.execute(
            f"SELECT id, source_type, content, topic_hint, domain_hint FROM raw_events WHERE id IN ({placeholders}) ORDER BY id",
            tuple(source_ids),
        ).fetchall()
        for raw_event_id, source_type, raw_content, topic_hint, domain_hint in rows:
            content = sentence(str(raw_content or ""), limit=420)
            source_type = str(source_type or "")
            if not content or is_question(content):
                skipped.append({"raw_event_id": raw_event_id, "reason": "question_or_empty"})
                continue
            if source_type == "conversation-assistant" and not include_assistant:
                skipped.append({"raw_event_id": raw_event_id, "reason": "assistant_content_disabled"})
                continue
            title = str(topic_hint or first_sentence(content)[:80] or f"raw-event-{raw_event_id}")
            classified = classify(title, content, str(sid), str(name or "Unknown"))
            uncertainty = 0.7 if UNCERTAIN.search(content) else max(0.05, 1.0 - float(classified["classification_confidence"]))
            confidence = min(float(classified["suggested_payload"]["confidence"]), 0.25) if source_type == "conversation-assistant" else float(classified["suggested_payload"]["confidence"])
            digest = sha256_text(content)
            unit_key = sha256_text(f"{sid}:{raw_event_id}:{digest}")
            if not dry_run:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO memory_units(unit_key, subject_id, subject_name, session_id, session_card_id,
                    raw_event_id, source_event_ids, unit_kind, topic, content, content_hash, confidence, uncertainty,
                    importance, sensitivity, source_type, status)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (unit_key, sid, name, session_id, cid, raw_event_id, json.dumps([raw_event_id]), str(classified["underlying_long_term_kind"]), str(topic_hint or classified["suggested_payload"]["topic"]), content, digest, confidence, round(uncertainty, 3), float(classified["suggested_payload"]["importance"]), sensitivity(content), source_type),
                )
                if cursor.rowcount:
                    created.append({"unit_id": int(cursor.lastrowid), "raw_event_id": raw_event_id, "topic": str(topic_hint or classified["suggested_payload"]["topic"]), "confidence": confidence})
            else:
                created.append({"unit_id": None, "raw_event_id": raw_event_id, "topic": str(topic_hint or classified["suggested_payload"]["topic"]), "confidence": confidence})
        if not dry_run:
            conn.execute("UPDATE session_cards SET needs_extraction=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (cid,))
    if not dry_run:
        conn.commit()
    conn.close()
    return {"status": "ok", "dry_run": dry_run, "created": created, "skipped": skipped, "card_count": len(cards)}


def main() -> None:
    args = parse_args()
    emit(extract_units(store_root(args.store), subject_id=args.subject_id, card_id=args.card_id, limit=args.limit, include_assistant=args.include_assistant, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
