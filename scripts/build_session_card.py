#!/usr/bin/env python3
"""Create incremental, source-linked session cards without promoting memories."""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root
from config import get
from runtime_identity import add_identity_args


QUESTION = re.compile(r"(?:[?？]\s*$|^(?:why|what|how|can you|could you|请问|为什么|怎么|如何|是否|能不能|可不可以))", re.I)
SECRET = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|bearer\s+|(?:api[_-]?key|token|cookie|password|secret)\s*[:=]\s*)[^\s,;]+")


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
    add_identity_args(parser)
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


def _safe_excerpt(value: str, limit: int) -> str:
    text = SECRET.sub(r"\1[redacted]", " ".join(value.split()))
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def event_summary(events: list[dict[str, object]]) -> tuple[str, list[str], str]:
    lines: list[str] = []
    questions: list[str] = []
    tools: list[str] = []
    for event in events:
        source = str(event["source_type"] or "").replace("_", "-").casefold()
        if "resource" in source or "agent-observation" in source or "subagent" in source:
            continue
        content = sentence(_safe_excerpt(str(event["content"]), 420))
        if not content:
            continue
        if "tool" in source:
            tools.append(f"- tool result: {_safe_excerpt(content, 240)}")
            continue
        role = "User" if source == "conversation-user" else "Assistant" if source == "conversation-assistant" else "Event"
        lines.append(f"- {role} summary: {content}")
        if source == "conversation-user" and QUESTION.search(content):
            questions.append(content)
    return "\n".join(lines)[-6000:], questions[:8], "\n".join(tools)[-1200:]


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
    profile_id: str | None = None,
    workspace_id: str | None = None,
    origin_agent_id: str | None = None,
) -> dict[str, object]:
    conn = open_db(root)
    completed_turn = "(turn_uid IS NULL OR EXISTS (SELECT 1 FROM turns AS t WHERE t.turn_uid=raw_events.turn_uid AND t.status='completed'))"
    clauses = ["processed_state IN ('pending', 'sessionized')", completed_turn]
    params: list[object] = []
    if subject_id:
        clauses.append("subject_id = ?")
        params.append(subject_id)
    if profile_id is not None:
        clauses.append("profile_id = ?")
        params.append(profile_id)
    if workspace_id is not None:
        clauses.append("workspace_id = ?")
        params.append(workspace_id)
    if origin_agent_id is not None:
        clauses.append("COALESCE(origin_agent_id, '') = ?")
        params.append(origin_agent_id)
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
        SELECT subject_id, MAX(subject_name), COALESCE(session_id, ''), profile_id, workspace_id, COALESCE(origin_agent_id, ''), COUNT(*)
        FROM raw_events
        WHERE {' AND '.join(clauses)}
        GROUP BY subject_id, COALESCE(session_id, ''), profile_id, workspace_id, COALESCE(origin_agent_id, '')
        ORDER BY MIN(id)
        """,
        tuple(params),
    ).fetchall()
    results: list[dict[str, object]] = []
    now = datetime.now(timezone.utc).isoformat()
    for raw_subject, raw_name, raw_session, raw_profile, raw_workspace, raw_agent, count in groups:
        sid = str(raw_subject or "")
        sess = str(raw_session or "")
        key = session_key(sess)
        card = conn.execute(
            "SELECT id, last_event_id, source_event_ids, summary, open_questions, version, last_extracted_event_id, tool_summary FROM session_cards WHERE subject_id = ? AND session_id = ? AND profile_id=? AND workspace_id=? AND COALESCE(origin_agent_id,'')=?",
            (sid, key, raw_profile, raw_workspace, raw_agent),
        ).fetchone()
        last_event_id = int(card[1] or 0) if card else 0
        events_raw = conn.execute(
            """
            SELECT id, source_type, content, created_at, event_time
            FROM raw_events
            WHERE subject_id = ? AND COALESCE(session_id, '') = ?
              AND profile_id=? AND workspace_id=? AND COALESCE(origin_agent_id,'')=?
              AND id > ? AND processed_state IN ('pending', 'sessionized')
              AND (turn_uid IS NULL OR EXISTS (SELECT 1 FROM turns AS t WHERE t.turn_uid=raw_events.turn_uid AND t.status='completed'))
            """ + (" AND id <= ?" if event_end_id is not None else "") + " ORDER BY id ASC LIMIT ?",
            (sid, sess, raw_profile, raw_workspace, raw_agent, max(last_event_id, (event_start_id or 0) - 1), *((event_end_id,) if event_end_id is not None else ()), max_events),
        ).fetchall()
        events = [
            {"id": int(row[0]), "source_type": str(row[1] or "conversation"), "content": str(row[2] or ""), "created_at": str(row[3] or ""), "event_time": str(row[4] or "")}
            for row in events_raw
        ]
        if not events or (len(events) < min_events and not force):
            results.append({"subject_id": sid, "session_id": sess, "created": False, "reason": "threshold_not_reached" if events else "no_new_events", "event_count": len(events)})
            continue
        addition, questions, tool_addition = event_summary(events)
        old_ids = json.loads(card[2] or "[]") if card else []
        old_questions = json.loads(card[4] or "[]") if card else []
        old_summary = str(card[3] or "") if card else ""
        old_tool_summary = str(card[7] or "") if card else ""
        # The normalized relation is authoritative.  Keep this legacy JSON
        # field bounded so a long-running conversation does not grow one row
        # without limit, while preserving a compact recent audit preview.
        ids = (old_ids + [event["id"] for event in events if event["id"] not in old_ids])[-200:]
        summary = "\n".join(part for part in [old_summary, addition] if part).strip()[-6000:]
        tool_summary = "\n".join(part for part in [old_tool_summary, tool_addition] if part).strip()[-1200:]
        open_questions = list(dict.fromkeys(old_questions + questions))[:12]
        completed = conn.execute(
            "SELECT COUNT(*),MAX(completed_at) FROM turns WHERE subject_id=? AND profile_id=? AND workspace_id=? AND origin_agent_id=? AND external_session_id=? AND status='completed'",
            (sid, raw_profile, raw_workspace, raw_agent, sess),
        ).fetchone()
        completed_count = int(completed[0] or 0)
        last_completed_at = str(completed[1] or "")
        if not dry_run:
            if card:
                card_id = int(card[0])
                conn.execute(
                    """
                    UPDATE session_cards SET event_end_id=?, last_event_id=?, source_event_ids=?, summary=?, tool_summary=?, open_questions=?,
                    completed_turn_count=?, last_completed_turn_at=?, summary_visibility='workspace', detail_visibility='workspace',
                    summary_generation=summary_generation+1, summary_dirty=0, needs_extraction=1, version=?, updated_at=? WHERE id=?
                    """,
                    (events[-1]["id"], events[-1]["id"], json.dumps(ids, ensure_ascii=False), summary, tool_summary, json.dumps(open_questions, ensure_ascii=False), completed_count, last_completed_at or None, int(card[5] or 1) + 1, now, card_id),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO session_cards(subject_id, subject_name, session_id, profile_id, workspace_id, origin_agent_id, event_start_id, event_end_id, last_event_id,
                    source_event_ids, summary, tool_summary, open_questions, completed_turn_count, last_completed_turn_at, summary_visibility, detail_visibility, summary_generation, summary_dirty, state, needs_extraction, version, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'workspace', 'workspace', 1, 0, 'active', 1, 1, ?)
                    """,
                    (sid, str(raw_name or "Unknown"), key, raw_profile, raw_workspace, raw_agent or origin_agent_id or "", events[0]["id"], events[-1]["id"], events[-1]["id"], json.dumps(ids, ensure_ascii=False), summary, tool_summary, json.dumps(open_questions, ensure_ascii=False), completed_count, last_completed_at or None, now),
                )
                card_id = int(cursor.lastrowid)
            placeholders = ", ".join("?" for _ in events)
            conn.execute(
                f"UPDATE raw_events SET processed_state='sessionized', session_card_id=?, sessionized_at=? WHERE id IN ({placeholders})",
                (card_id, now, *[event["id"] for event in events]),
            )
            conn.executemany("INSERT OR IGNORE INTO session_card_events(card_id, raw_event_id) VALUES(?, ?)", [(card_id, event["id"]) for event in events])
        results.append({"subject_id": sid, "session_id": sess, "origin_agent_id": str(raw_agent or ""), "card_id": card_id if not dry_run else (int(card[0]) if card else None), "created": not bool(card), "event_count": len(events), "source_event_ids": [event["id"] for event in events], "open_questions": questions})
    if not dry_run:
        conn.commit()
    conn.close()
    return {"status": "ok", "dry_run": dry_run, "cards": results}


def refresh_dirty_cards(
    root,
    *,
    profile_id: str | None = None,
    workspace_id: str | None = None,
    subject_id: str | None = None,
    limit: int = 20,
) -> dict[str, object]:
    """Regenerate legacy/dirty summaries only from safe completed evidence."""

    conn = open_db(root)
    clauses = ["summary_dirty=1"]
    params: list[object] = []
    for column, value in (("profile_id", profile_id), ("workspace_id", workspace_id), ("subject_id", subject_id)):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(value)
    cards = conn.execute(
        "SELECT id,subject_id,session_id,profile_id,workspace_id,COALESCE(origin_agent_id,'') FROM session_cards WHERE "
        + " AND ".join(clauses)
        + " ORDER BY updated_at LIMIT ?",
        (*params, max(1, limit)),
    ).fetchall()
    updated: list[dict[str, object]] = []
    now = datetime.now(timezone.utc).isoformat()
    try:
        for card_id, sid, session_id, profile, workspace, agent in cards:
            rows = conn.execute(
                """
                SELECT r.id,r.source_type,r.content,r.created_at,r.event_time
                FROM session_card_events AS link
                JOIN raw_events AS r ON r.id=link.raw_event_id
                LEFT JOIN turns AS t ON t.turn_uid=r.turn_uid
                WHERE link.card_id=? AND (r.turn_uid IS NULL OR t.status='completed')
                ORDER BY r.id
                """,
                (card_id,),
            ).fetchall()
            events = [
                {"id": int(row[0]), "source_type": str(row[1] or "conversation"), "content": str(row[2] or ""), "created_at": str(row[3] or ""), "event_time": str(row[4] or "")}
                for row in rows
            ]
            summary, questions, tool_summary = event_summary(events)
            completed = conn.execute(
                "SELECT COUNT(*),MAX(completed_at) FROM turns WHERE subject_id=? AND profile_id=? AND workspace_id=? AND origin_agent_id=? AND external_session_id=? AND status='completed'",
                (sid, profile, workspace, agent, session_id),
            ).fetchone()
            conn.execute(
                """
                UPDATE session_cards SET source_event_ids=?,summary=?,tool_summary=?,open_questions=?,
                    completed_turn_count=?,last_completed_turn_at=?,summary_visibility='workspace',detail_visibility='workspace',
                    summary_generation=summary_generation+1,summary_dirty=0,updated_at=? WHERE id=?
                """,
                (
                    json.dumps([event["id"] for event in events][-200:], ensure_ascii=False), summary, tool_summary,
                    json.dumps(questions, ensure_ascii=False), int(completed[0] or 0), str(completed[1] or "") or None,
                    now, card_id,
                ),
            )
            updated.append({"card_id": int(card_id), "session_id": str(session_id), "event_count": len(events), "completed_turns": int(completed[0] or 0)})
        conn.commit()
        return {"status": "ok", "cards": updated}
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    emit(build_cards(store_root(args.store), subject_id=args.subject_id, session_id=args.session_id, min_events=max(1, args.min_events), max_events=max(1, args.max_events), force=args.force, dry_run=args.dry_run, event_start_id=args.event_start_id, event_end_id=args.event_end_id, profile_id=args.profile_id, workspace_id=args.workspace_id, origin_agent_id=args.agent_id or None))


if __name__ == "__main__":
    main()
