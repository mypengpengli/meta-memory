"""Durable, source-aware storage of original conversation messages."""
from __future__ import annotations

import json
from pathlib import Path

from _common import open_db, sha256_text, utc_now


SOURCE_PRIORITY = {
    "interactive": 1.0,
    "gateway": 0.95,
    "imported": 0.8,
    "cron": 0.45,
    "tool": 0.2,
    "subagent": 0.15,
}
HIDDEN_SOURCES = {"tool", "subagent"}


def source_from_event(source_type: str) -> str:
    raw = (source_type or "").casefold()
    if "cron" in raw:
        return "cron"
    if "tool" in raw:
        return "tool"
    if "subagent" in raw:
        return "subagent"
    if "import" in raw:
        return "imported"
    if "gateway" in raw:
        return "gateway"
    return "interactive"


def role_from_event(source_type: str) -> str:
    raw = (source_type or "").casefold()
    if "assistant" in raw:
        return "assistant"
    if "tool" in raw:
        return "tool"
    if "system" in raw:
        return "system"
    return "user"


def _session_key(session_id: str, subject_id: str) -> str:
    return session_id.strip() or f"implicit:{subject_id}"


def ensure_session(
    root: Path,
    *,
    subject_id: str,
    session_id: str,
    profile_id: str = "default",
    workspace_id: str = "default",
    source: str = "interactive",
    parent_session_id: str = "",
    title: str = "",
    metadata: dict[str, object] | None = None,
) -> str:
    key = _session_key(session_id, subject_id)
    conn = open_db(root)
    conn.execute(
        """
        INSERT INTO sessions(session_id, parent_session_id, subject_id, profile_id, workspace_id, source, title, started_at, last_active_at, metadata_json)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            last_active_at=excluded.last_active_at,
            title=CASE WHEN excluded.title != '' THEN excluded.title ELSE sessions.title END,
            parent_session_id=CASE WHEN sessions.parent_session_id IS NULL OR sessions.parent_session_id='' THEN excluded.parent_session_id ELSE sessions.parent_session_id END
        """,
        (key, parent_session_id or None, subject_id, profile_id, workspace_id, source, title or None, utc_now(), utc_now(), json.dumps(metadata or {}, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
    return key


def record_session_message(
    root: Path,
    *,
    subject_id: str,
    session_id: str,
    source_type: str,
    content: str,
    raw_event_id: int | None = None,
    timestamp: str = "",
    profile_id: str = "default",
    workspace_id: str = "default",
    parent_session_id: str = "",
    tool_name: str = "",
    tool_call_id: str = "",
    tool_calls: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    source = source_from_event(source_type)
    key = ensure_session(root, subject_id=subject_id, session_id=session_id, profile_id=profile_id, workspace_id=workspace_id, source=source, parent_session_id=parent_session_id)
    conn = open_db(root)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO session_messages(session_id, raw_event_id, role, content, tool_name, tool_call_id, tool_calls_json, timestamp, content_hash)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (key, raw_event_id, role_from_event(source_type), content or "", tool_name or None, tool_call_id or None, json.dumps(tool_calls or [], ensure_ascii=False), timestamp or utc_now(), sha256_text(content or "")),
    )
    message_id = int(cursor.lastrowid) if cursor.rowcount else None
    if message_id is not None:
        try:
            conn.execute("INSERT INTO session_messages_fts(rowid, content, tool_name) VALUES(?, ?, ?)", (message_id, content or "", tool_name or ""))
        except Exception:
            pass
    conn.execute("UPDATE sessions SET last_active_at=? WHERE session_id=?", (utc_now(), key))
    conn.commit()
    conn.close()
    return {"session_id": key, "message_id": message_id, "source": source}


def close_session(root: Path, session_id: str) -> None:
    conn = open_db(root)
    conn.execute("UPDATE sessions SET status='ended', ended_at=?, last_active_at=? WHERE session_id=?", (utc_now(), utc_now(), session_id))
    conn.commit()
    conn.close()


def record_messages(root: Path, *, subject_id: str, session_id: str, messages: list[dict[str, object]], profile_id: str = "default", workspace_id: str = "default") -> list[int]:
    stored: list[int] = []
    for message in messages:
        result = record_session_message(
            root,
            subject_id=subject_id,
            session_id=session_id,
            source_type=str(message.get("source_type") or message.get("role") or "conversation"),
            content=str(message.get("content") or ""),
            raw_event_id=int(message["raw_event_id"]) if str(message.get("raw_event_id", "")).isdigit() else None,
            timestamp=str(message.get("timestamp") or ""),
            profile_id=profile_id,
            workspace_id=workspace_id,
            parent_session_id=str(message.get("parent_session_id") or ""),
            tool_name=str(message.get("tool_name") or ""),
            tool_call_id=str(message.get("tool_call_id") or ""),
            tool_calls=list(message.get("tool_calls") or []),
        )
        if result["message_id"]:
            stored.append(int(result["message_id"]))
    return stored
