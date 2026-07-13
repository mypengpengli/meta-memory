#!/usr/bin/env python3
"""Create incremental, source-linked session cards without promoting memories."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root
from config import get


QUESTION = re.compile(r"(?:[?？]\s*$|^(?:why|what|how|can you|could you|请问|为什么|怎么|如何|是否|能不能|可不可以))", re.I)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build or incrementally update session cards from raw events.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--subject-id", help="Only process this subject")
    parser.add_argument("--session-id", help="Only process this session")
    parser.add_argument("--min-events", type=int, default=int(get("heartbeat.session_flush_min_events")), help="Minimum uncarded events")
    parser.add_argument("--force", action="store_true", help="Build cards even below the threshold")
    parser.add_argument("--max-events", type=int, default=100, help="Maximum events per card update")
    parser.add_argument("--event-start-id", type=int)
    parser.add_argument("--event-end-id", type=int)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def sentence(value: str, limit: int = 260) -> str:
    text = " ".join(value.split())
    if not text:
        return ""
    match = re.search(r"[。！？!?](?:\s|$)", text)
    result = text[: match.end()].strip() if match else text
    return result if len(result) <= limit else result[: limit - 1].rstrip() + "…"


def session_key(session_id: str) -> str:
    return session_id.strip() or "__default__"


def event_summary(events: list[dict[str, object]]) -> tuple[str, list[str]]:
    lines: list[str] = []
    questions: list[str] = []
    for event in events:
        content = sentence(str(event["content"]))
        if not content:
            continue
        lines.append(f"- [raw_event:{event['id']}] {event['source_type']}: {content}")
        if str(event["source_type"]) == "conversation-user" and QUESTION.search(content):
            questions.append(content)
    return "\n".join(lines), questions[:8]


def build_cards(
    root,
    *,
    subject_id: str | None = None,
    session_id: str | None = None,
    min_events: int = 5,
    max_events: int = 100,
    force: bool = False,
    dry_run: bool = False,
    event_start_id: int | None = None,
    event_end_id: int | None = None,
) -> dict[str, object]:
    conn = open_db(root)
    clauses = ["processed_state IN ('pending', 'sessionized')"]
    params: list[object] = []
    if subject_id:
        clauses.append("subject_id = ?")
        params.append(subject_id)
    if session_id is not None:
        clauses.append("COALESCE(session_id, '') = ?")
        params.append(session_id)
    if event_start_id is not None:
        clauses.append("id >= ?")
        params.append(event_start_id)
    if event_end_id is not None:
        clauses.append("id <= ?")
        params.append(event_end_id)
    groups = conn.execute(
        f"""
        SELECT subject_id, MAX(subject_name), COALESCE(session_id, ''), COUNT(*)
        FROM raw_events
        WHERE {' AND '.join(clauses)}
        GROUP BY subject_id, COALESCE(session_id, '')
        ORDER BY MIN(id)
        """,
        tuple(params),
    ).fetchall()
    results: list[dict[str, object]] = []
    now = datetime.now(timezone.utc).isoformat()
    for raw_subject, raw_name, raw_session, count in groups:
        sid = str(raw_subject or "")
        sess = str(raw_session or "")
        key = session_key(sess)
        card = conn.execute(
            "SELECT id, last_event_id, source_event_ids, summary, open_questions, version, last_extracted_event_id FROM session_cards WHERE subject_id = ? AND session_id = ?",
            (sid, key),
        ).fetchone()
        last_event_id = int(card[1] or 0) if card else 0
        events_raw = conn.execute(
            """
            SELECT id, source_type, content, created_at, event_time
            FROM raw_events
            WHERE subject_id = ? AND COALESCE(session_id, '') = ?
              AND id > ? AND processed_state IN ('pending', 'sessionized')
            ORDER BY id ASC LIMIT ?
            """,
            (sid, sess, max(last_event_id, (event_start_id or 0) - 1), max_events),
        ).fetchall()
        events = [
            {"id": int(row[0]), "source_type": str(row[1] or "conversation"), "content": str(row[2] or ""), "created_at": str(row[3] or ""), "event_time": str(row[4] or "")}
            for row in events_raw
        ]
        if not events or (len(events) < min_events and not force):
            results.append({"subject_id": sid, "session_id": sess, "created": False, "reason": "threshold_not_reached" if events else "no_new_events", "event_count": len(events)})
            continue
        addition, questions = event_summary(events)
        old_ids = json.loads(card[2] or "[]") if card else []
        old_questions = json.loads(card[4] or "[]") if card else []
        old_summary = str(card[3] or "") if card else ""
        ids = old_ids + [event["id"] for event in events if event["id"] not in old_ids]
        summary = "\n".join(part for part in [old_summary, addition] if part).strip()[-12000:]
        open_questions = list(dict.fromkeys(old_questions + questions))[:12]
        if not dry_run:
            if card:
                card_id = int(card[0])
                conn.execute(
                    """
                    UPDATE session_cards SET event_end_id=?, last_event_id=?, source_event_ids=?, summary=?, open_questions=?,
                    needs_extraction=1, version=?, updated_at=? WHERE id=?
                    """,
                    (events[-1]["id"], events[-1]["id"], json.dumps(ids, ensure_ascii=False), summary, json.dumps(open_questions, ensure_ascii=False), int(card[5] or 1) + 1, now, card_id),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO session_cards(subject_id, subject_name, session_id, event_start_id, event_end_id, last_event_id,
                    source_event_ids, summary, open_questions, state, needs_extraction, version, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, 1, ?)
                    """,
                    (sid, str(raw_name or "Unknown"), key, events[0]["id"], events[-1]["id"], events[-1]["id"], json.dumps(ids, ensure_ascii=False), summary, json.dumps(open_questions, ensure_ascii=False), now),
                )
                card_id = int(cursor.lastrowid)
            placeholders = ", ".join("?" for _ in events)
            conn.execute(
                f"UPDATE raw_events SET processed_state='sessionized', session_card_id=?, sessionized_at=? WHERE id IN ({placeholders})",
                (card_id, now, *[event["id"] for event in events]),
            )
        results.append({"subject_id": sid, "session_id": sess, "card_id": card_id if not dry_run else (int(card[0]) if card else None), "created": not bool(card), "event_count": len(events), "source_event_ids": [event["id"] for event in events], "open_questions": questions})
    if not dry_run:
        conn.commit()
    conn.close()
    return {"status": "ok", "dry_run": dry_run, "cards": results}


def main() -> None:
    args = parse_args()
    emit(build_cards(store_root(args.store), subject_id=args.subject_id, session_id=args.session_id, min_events=max(1, args.min_events), max_events=max(1, args.max_events), force=args.force, dry_run=args.dry_run, event_start_id=args.event_start_id, event_end_id=args.event_end_id))


if __name__ == "__main__":
    main()
