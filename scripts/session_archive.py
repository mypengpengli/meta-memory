"""Scope-safe session archive; one transaction can include raw/event/message."""
from __future__ import annotations

import json
import uuid
from typing import Any

from _common import open_db, sha256_text, utc_now


SOURCE_PRIORITY = {"interactive":1.0,"gateway":.95,"imported":.8,"cron":.45,"tool":.2,"subagent":.15,"agent-observation":.15,"resource":.1}
# Tool calls, subagent chatter, and imported documents are auditable records,
# not conversational recall.  They must never become an implicit prompt path.
HIDDEN_SOURCES = {"tool", "subagent", "agent-observation", "resource"}


def source_from_event(source_type: str) -> str:
    value = (source_type or "").casefold().replace("_", "-")
    compact = value.replace("-", "")
    return "resource" if "resource" in value else "agent-observation" if "agent-observation" in value else "cron" if "cron" in value else "tool" if "tool" in value else "subagent" if "subagent" in compact else "imported" if "import" in value else "gateway" if "gateway" in value else "interactive"


def role_from_event(source_type: str) -> str:
    value = (source_type or "").casefold()
    source = source_from_event(value)
    return "assistant" if "assistant" in value else "tool" if source == "tool" else "system" if source in HIDDEN_SOURCES or "system" in value else "user"


def external_session_id(session_id: str, subject_id: str) -> str:
    return session_id.strip() or f"implicit:{sha256_text(subject_id)[:16]}"


def scope_key(*, workspace_id: str, profile_id: str, subject_id: str, session_id: str, origin_agent_id: str = "") -> str:
    return sha256_text(json.dumps([workspace_id, profile_id, subject_id, origin_agent_id or "", external_session_id(session_id, subject_id)], ensure_ascii=False, separators=(",", ":")))


def ensure_session(root, *, subject_id: str, session_id: str, profile_id: str = "default", workspace_id: str = "default", origin_agent_id: str = "", source: str = "interactive", parent_session_id: str = "", title: str = "", metadata: dict[str, object] | None = None, shared_mode: bool = False, conn=None) -> str:
    if shared_mode and not session_id.strip():
        raise ValueError("A stable unique session_id is required in shared mode.")
    own = conn is None
    conn = conn or open_db(root)
    external = external_session_id(session_id, subject_id)
    agent = str(origin_agent_id or "")
    scope = scope_key(workspace_id=workspace_id, profile_id=profile_id, subject_id=subject_id, session_id=session_id, origin_agent_id=agent)
    row = conn.execute("SELECT session_id, subject_id, workspace_id, profile_id, origin_agent_id FROM sessions WHERE scope_key=?", (scope,)).fetchone()
    if not row:
        # 017 could backfill the Agent column for an old archive but cannot
        # calculate this Python SHA-256 scope key in portable SQLite.  Adopt a
        # uniquely matching legacy row on first use instead of duplicating its
        # transcript under the same external session id.
        legacy = conn.execute(
            "SELECT session_id, subject_id, workspace_id, profile_id, origin_agent_id FROM sessions "
            "WHERE external_session_id=? AND subject_id=? AND workspace_id=? AND profile_id=? AND origin_agent_id=?",
            (external, subject_id, workspace_id, profile_id, agent),
        ).fetchone()
        if legacy:
            conn.execute("UPDATE sessions SET scope_key=? WHERE session_id=?", (scope, str(legacy[0])))
            row = legacy
    if row:
        if tuple(str(value) for value in row[1:]) != (subject_id, workspace_id, profile_id, agent):
            raise ValueError("Session scope collision detected")
        internal = str(row[0])
        conn.execute("UPDATE sessions SET last_active_at=?, title=CASE WHEN ?!='' THEN ? ELSE title END WHERE session_id=?", (utc_now(), title, title, internal))
    else:
        parent = ""
        if parent_session_id:
            parent_scope = scope_key(workspace_id=workspace_id, profile_id=profile_id, subject_id=subject_id, session_id=parent_session_id, origin_agent_id=agent)
            parent_row = conn.execute("SELECT session_id FROM sessions WHERE scope_key=?", (parent_scope,)).fetchone()
            parent = str(parent_row[0]) if parent_row else ""
        internal = str(uuid.uuid4())
        session_metadata = {**(metadata or {}), "origin_agent_id": agent}
        conn.execute("INSERT INTO sessions(session_id, external_session_id, scope_key, parent_session_id, subject_id, profile_id, workspace_id, origin_agent_id, source, title, started_at, last_active_at, metadata_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (internal, external, scope, parent or None, subject_id, profile_id, workspace_id, agent, source, title or None, utc_now(), utc_now(), json.dumps(session_metadata, ensure_ascii=False)))
    if own: conn.commit(); conn.close()
    return internal


def resolve_session(root, *, subject_id: str, session_id: str, profile_id: str = "default", workspace_id: str = "default", origin_agent_id: str = "", conn=None) -> str | None:
    own = conn is None; conn = conn or open_db(root)
    # Allow callers to pass the opaque internal id returned by discovery.
    agent = str(origin_agent_id or "")
    row = conn.execute("SELECT session_id FROM sessions WHERE session_id=? AND subject_id=? AND profile_id=? AND workspace_id=? AND origin_agent_id=?", (session_id, subject_id, profile_id, workspace_id, agent)).fetchone()
    if not row:
        row = conn.execute("SELECT session_id FROM sessions WHERE scope_key=?", (scope_key(workspace_id=workspace_id, profile_id=profile_id, subject_id=subject_id, session_id=session_id, origin_agent_id=agent),)).fetchone()
    if not row:
        row = conn.execute(
            "SELECT session_id FROM sessions WHERE external_session_id=? AND subject_id=? AND profile_id=? AND workspace_id=? AND origin_agent_id=?",
            (external_session_id(session_id, subject_id), subject_id, profile_id, workspace_id, agent),
        ).fetchone()
    if own: conn.close()
    return str(row[0]) if row else None


def record_session_message(root, *, subject_id: str, session_id: str, source_type: str, content: str, raw_event_id: int | None = None, timestamp: str = "", profile_id: str = "default", workspace_id: str = "default", origin_agent_id: str = "", parent_session_id: str = "", tool_name: str = "", tool_call_id: str = "", tool_calls: list[dict[str, object]] | None = None, shared_mode: bool = False, conn=None) -> dict[str, object]:
    own = conn is None; conn = conn or open_db(root)
    internal = ensure_session(root, subject_id=subject_id, session_id=session_id, profile_id=profile_id, workspace_id=workspace_id, origin_agent_id=origin_agent_id, source=source_from_event(source_type), parent_session_id=parent_session_id, shared_mode=shared_mode, conn=conn)
    cursor = conn.execute("INSERT OR IGNORE INTO session_messages(session_id, raw_event_id, role, content, tool_name, tool_call_id, tool_calls_json, timestamp, content_hash) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)", (internal, raw_event_id, role_from_event(source_type), content or "", tool_name or None, tool_call_id or None, json.dumps(tool_calls or [], ensure_ascii=False), timestamp or utc_now(), sha256_text(content or "")))
    message_id = int(cursor.lastrowid) if cursor.rowcount else None
    if message_id is not None:
        try: conn.execute("INSERT INTO session_messages_fts(rowid, content, tool_name) VALUES(?, ?, ?)", (message_id, content or "", tool_name or ""))
        except Exception: pass
    conn.execute("UPDATE sessions SET last_active_at=? WHERE session_id=?", (utc_now(), internal))
    if own: conn.commit(); conn.close()
    return {"session_id": internal,"external_session_id":external_session_id(session_id, subject_id),"message_id":message_id,"source":source_from_event(source_type)}


def close_session(root, session_id: str, *, subject_id: str, profile_id: str = "default", workspace_id: str = "default", origin_agent_id: str = "") -> None:
    conn = open_db(root); internal = resolve_session(root, subject_id=subject_id, session_id=session_id, profile_id=profile_id, workspace_id=workspace_id, origin_agent_id=origin_agent_id, conn=conn)
    if internal: conn.execute("UPDATE sessions SET status='ended', ended_at=?, last_active_at=? WHERE session_id=?", (utc_now(), utc_now(), internal))
    conn.commit(); conn.close()


def record_messages(root, *, subject_id: str, session_id: str, messages: list[dict[str, object]], profile_id: str = "default", workspace_id: str = "default", origin_agent_id: str = "") -> list[int]:
    conn = open_db(root); stored = []
    try:
        for message in messages:
            result = record_session_message(root, subject_id=subject_id, session_id=session_id, source_type=str(message.get("source_type") or message.get("role") or "conversation"), content=str(message.get("content") or ""), raw_event_id=int(message["raw_event_id"]) if str(message.get("raw_event_id", "")).isdigit() else None, timestamp=str(message.get("timestamp") or ""), profile_id=profile_id, workspace_id=workspace_id, origin_agent_id=str(message.get("origin_agent_id") or origin_agent_id), parent_session_id=str(message.get("parent_session_id") or ""), tool_name=str(message.get("tool_name") or ""), tool_call_id=str(message.get("tool_call_id") or ""), tool_calls=list(message.get("tool_calls") or []), conn=conn)
            if result["message_id"]: stored.append(int(result["message_id"]))
        conn.commit()
    finally: conn.close()
    return stored
