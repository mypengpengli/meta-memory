#!/usr/bin/env python3
"""Hermes-style discovery, scroll, and browse for archived raw sessions."""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root
from session_archive import HIDDEN_SOURCES, SOURCE_PRIORITY


def _terms(query: str) -> list[str]:
    return [term for term in re.findall(r"[\w.-]{2,}|[\u4e00-\u9fff]{2,}", query.casefold()) if term]


def _root_for(sessions: dict[str, str], session_id: str) -> str:
    seen: set[str] = set()
    current = session_id
    while sessions.get(current) and current not in seen:
        seen.add(current)
        current = sessions[current]
    return current


def _message(row) -> dict[str, object]:
    return {"id": int(row[0]), "session_id": str(row[1]), "role": str(row[2]), "content": str(row[3]), "tool_name": str(row[4] or ""), "timestamp": str(row[5])}


def discovery(root: Path, *, subject_id: str, query: str, limit: int = 10, include_hidden: bool = False) -> dict[str, object]:
    conn = open_db(root)
    terms = _terms(query)
    sessions = {str(row[0]): str(row[1] or "") for row in conn.execute("SELECT session_id, parent_session_id FROM sessions WHERE subject_id=?", (subject_id,))}
    clauses = ["s.subject_id=?"]
    params: list[object] = [subject_id]
    for term in terms[:8]:
        clauses.append("LOWER(m.content) LIKE ?")
        params.append(f"%{term}%")
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT m.id, m.session_id, m.role, m.content, m.tool_name, m.timestamp, s.source, s.title, s.last_active_at
        FROM session_messages AS m JOIN sessions AS s ON s.session_id=m.session_id
        WHERE {where}
        ORDER BY s.last_active_at DESC, m.id DESC
        LIMIT ?
        """,
        (*params, max(limit * 8, 20)),
    ).fetchall()
    grouped: dict[str, dict[str, object]] = {}
    for row in rows:
        source = str(row[6] or "interactive")
        if not include_hidden and source in HIDDEN_SOURCES:
            continue
        root_id = _root_for(sessions, str(row[1]))
        item = grouped.get(root_id)
        score = SOURCE_PRIORITY.get(source, 0.5) + min(0.3, sum(1 for term in terms if term in str(row[3]).casefold()) * 0.05)
        if item is None or score > float(item["score"]):
            grouped[root_id] = {"session_id": str(row[1]), "lineage_root": root_id, "title": str(row[7] or ""), "source": source, "match_message_id": int(row[0]), "match_snippet": " ".join(str(row[3]).split())[:280], "last_active_at": str(row[8]), "score": score}
    results = sorted(grouped.values(), key=lambda item: (float(item["score"]), str(item["last_active_at"])), reverse=True)[:limit]
    for item in results:
        item.pop("score", None)
        item["window"] = scroll(root, session_id=str(item["session_id"]), around_message_id=int(item["match_message_id"]), window=2)["messages"]
    conn.close()
    return {"status": "ok", "mode": "discovery", "query": query, "sessions": results}


def scroll(root: Path, *, session_id: str, around_message_id: int, window: int = 6) -> dict[str, object]:
    conn = open_db(root)
    bounds = conn.execute("SELECT id FROM session_messages WHERE session_id=? AND id<=? ORDER BY id DESC LIMIT ?", (session_id, around_message_id, max(window + 1, 1))).fetchall()
    before = [int(row[0]) for row in bounds]
    after = conn.execute("SELECT id FROM session_messages WHERE session_id=? AND id>? ORDER BY id LIMIT ?", (session_id, around_message_id, max(window, 0))).fetchall()
    ids = sorted(before + [int(row[0]) for row in after])
    if not ids:
        conn.close()
        return {"status": "ok", "mode": "scroll", "session_id": session_id, "messages": []}
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(f"SELECT id, session_id, role, content, tool_name, timestamp FROM session_messages WHERE id IN ({placeholders}) ORDER BY id", ids).fetchall()
    conn.close()
    return {"status": "ok", "mode": "scroll", "session_id": session_id, "messages": [_message(row) for row in rows]}


def browse(root: Path, *, subject_id: str, recent: int = 20, include_hidden: bool = False) -> dict[str, object]:
    conn = open_db(root)
    rows = conn.execute("SELECT session_id, parent_session_id, source, title, started_at, last_active_at, status FROM sessions WHERE subject_id=? ORDER BY last_active_at DESC LIMIT ?", (subject_id, max(recent * 3, recent))).fetchall()
    results: list[dict[str, object]] = []
    seen_roots: set[str] = set()
    lineage = {str(row[0]): str(row[1] or "") for row in rows}
    for row in rows:
        source = str(row[2] or "interactive")
        if not include_hidden and source in HIDDEN_SOURCES:
            continue
        root_id = _root_for(lineage, str(row[0]))
        if root_id in seen_roots:
            continue
        seen_roots.add(root_id)
        results.append({"session_id": str(row[0]), "lineage_root": root_id, "source": source, "title": str(row[3] or ""), "started_at": str(row[4]), "last_active_at": str(row[5]), "status": str(row[6])})
        if len(results) >= recent:
            break
    conn.close()
    return {"status": "ok", "mode": "browse", "sessions": results}


def main() -> None:
    parser = argparse.ArgumentParser(description="Search original messages without an LLM.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--subject-id")
    parser.add_argument("--query")
    parser.add_argument("--session-id")
    parser.add_argument("--around-message-id", type=int)
    parser.add_argument("--window", type=int, default=6)
    parser.add_argument("--recent", type=int)
    parser.add_argument("--include-hidden", action="store_true")
    args = parser.parse_args()
    root = store_root(args.store)
    if args.session_id and args.around_message_id is not None:
        emit(scroll(root, session_id=args.session_id, around_message_id=args.around_message_id, window=args.window))
    elif args.recent is not None and args.subject_id:
        emit(browse(root, subject_id=args.subject_id, recent=args.recent, include_hidden=args.include_hidden))
    elif args.query and args.subject_id:
        emit(discovery(root, subject_id=args.subject_id, query=args.query, include_hidden=args.include_hidden))
    else:
        raise SystemExit("Use --subject-id with --query or --recent, or --session-id with --around-message-id.")


if __name__ == "__main__":
    main()
