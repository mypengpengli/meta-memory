#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, sha256_text, store_root
from runtime_identity import validate_visibility

LOGGER = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest a raw event into the event inbox without organizing it yet.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--subject-id", default="person-unknown", help="Primary subject id")
    parser.add_argument("--subject-name", default="Unknown", help="Primary subject display name")
    parser.add_argument("--session-id", default="", help="Session id for grouping recent events")
    parser.add_argument("--profile-id", default="default", help="Profile scope for the session projection")
    parser.add_argument("--workspace-id", default="default", help="Workspace scope for the session projection")
    parser.add_argument("--agent-id", default="", help="Originating agent identity")
    parser.add_argument("--visibility-scope", choices=["global", "workspace", "agent"], default="workspace")
    parser.add_argument("--event-uid", default="")
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--turn-uid", default="")
    parser.add_argument("--message-role", default="")
    parser.add_argument("--message-sequence", type=int)
    parser.add_argument("--shared-mode", action="store_true", help="Reject an empty session id in shared deployments")
    parser.add_argument("--source-type", default="conversation", help="Source type such as conversation, note, log")
    parser.add_argument("--source-ref", default="", help="Optional source reference or external id")
    parser.add_argument("--topic-hint", default="", help="Optional topic hint")
    parser.add_argument("--domain-hint", default="", help="Optional domain hint")
    parser.add_argument("--event-time", default="", help="Event time in ISO-like text")
    parser.add_argument("--content", help="Inline raw content")
    parser.add_argument("--content-file", help="Read raw content from a UTF-8 text file")
    parser.add_argument("--payload-file", help="Read event payload from a UTF-8 JSON file")
    parser.add_argument(
        "--allow-duplicate",
        action="store_true",
        help="Allow exact duplicate events for the same subject/session/source",
    )
    return parser.parse_args()


def load_payload(path: str | None) -> dict[str, object]:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def arg_or_payload(args: argparse.Namespace, payload: dict[str, object], attr: str, default: object = "") -> object:
    value = getattr(args, attr)
    if value not in (None, "", []):
        return value
    return payload.get(attr, default)


def read_content(args: argparse.Namespace, payload: dict[str, object]) -> str:
    if args.content_file:
        return Path(args.content_file).read_text(encoding="utf-8-sig").strip()
    if args.content:
        return args.content.strip()
    return str(payload.get("content", "")).strip()


def _duplicate_result(duplicate, *, subject_id: str, content_hash: str) -> dict[str, object]:
    return {
        "status": "ok",
        "inserted": False,
        "duplicate_of": {
            "id": int(duplicate[0]),
            "created_at": duplicate[1],
            "processed_state": duplicate[2],
        },
        "raw_event_id": int(duplicate[0]),
        "subject_id": subject_id,
        "content_hash": content_hash,
    }


def _find_duplicate(
    conn,
    *,
    subject_id: str,
    session_id: str,
    source_type: str,
    source_ref: str,
    content_hash: str,
    profile_id: str,
    workspace_id: str,
    origin_agent_id: str,
    idempotency_key: str,
    allow_duplicate: bool,
):
    """Choose exactly one deduplication strategy."""
    if allow_duplicate:
        return None
    if idempotency_key:
        return conn.execute(
            "SELECT id, created_at, processed_state FROM raw_events "
            "WHERE profile_id=? AND workspace_id=? AND origin_agent_id=? AND idempotency_key=? LIMIT 1",
            (profile_id, workspace_id, origin_agent_id, idempotency_key),
        ).fetchone()
    return conn.execute(
        """
        SELECT id, created_at, processed_state
        FROM raw_events
        WHERE subject_id=? AND profile_id=? AND workspace_id=?
          AND content_hash=?
          AND COALESCE(session_id, '')=?
          AND COALESCE(source_ref, '')=?
          AND COALESCE(source_type, '')=?
          AND COALESCE(origin_agent_id, '')=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (subject_id, profile_id, workspace_id, content_hash, session_id, source_ref, source_type, origin_agent_id),
    ).fetchone()


def insert_raw_event_with_conn(
    conn,
    root: Path,
    *,
    subject_id: str,
    subject_name: str,
    session_id: str = "",
    source_type: str = "conversation",
    source_ref: str = "",
    topic_hint: str = "",
    domain_hint: str = "",
    event_time: str = "",
    content: str,
    allow_duplicate: bool = False,
    profile_id: str = "default",
    workspace_id: str = "default",
    origin_agent_id: str = "",
    visibility_scope: str = "workspace",
    event_uid: str = "",
    idempotency_key: str = "",
    turn_uid: str = "",
    message_role: str = "",
    message_sequence: int | None = None,
    shared_mode: bool = False,
) -> dict[str, object]:
    """Insert one raw event into a caller-owned transaction.

    The helper never commits or closes the connection.  Turn creation can
    therefore persist the turn row and its user evidence atomically.
    """
    if not content.strip():
        raise ValueError("Raw event content must not be empty.")
    visibility_scope = validate_visibility(visibility_scope, origin_agent_id if visibility_scope == "agent" else "")
    content_hash = sha256_text(content)
    duplicate = _find_duplicate(
        conn,
        subject_id=subject_id,
        session_id=session_id,
        source_type=source_type,
        source_ref=source_ref,
        content_hash=content_hash,
        profile_id=profile_id,
        workspace_id=workspace_id,
        origin_agent_id=origin_agent_id,
        idempotency_key=idempotency_key,
        allow_duplicate=allow_duplicate,
    )
    if duplicate:
        return _duplicate_result(duplicate, subject_id=subject_id, content_hash=content_hash)

    try:
        cursor = conn.execute(
            """
            INSERT INTO raw_events(
                subject_id, subject_name, session_id, source_type, source_ref,
                content, content_hash, topic_hint, domain_hint, event_time, processed_state,
                profile_id, workspace_id, origin_agent_id, visibility_scope, event_uid,
                idempotency_key, turn_uid, message_role, message_sequence
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subject_id,
                subject_name,
                session_id,
                source_type,
                source_ref,
                content,
                content_hash,
                topic_hint,
                domain_hint,
                event_time,
                profile_id,
                workspace_id,
                origin_agent_id,
                visibility_scope,
                event_uid or None,
                idempotency_key or None,
                turn_uid or None,
                message_role or None,
                message_sequence,
            ),
        )
    except sqlite3.IntegrityError:
        duplicate = _find_duplicate(
            conn,
            subject_id=subject_id,
            session_id=session_id,
            source_type=source_type,
            source_ref=source_ref,
            content_hash=content_hash,
            profile_id=profile_id,
            workspace_id=workspace_id,
            origin_agent_id=origin_agent_id,
            idempotency_key=idempotency_key,
            allow_duplicate=False,
        )
        if duplicate:
            return _duplicate_result(duplicate, subject_id=subject_id, content_hash=content_hash)
        raise

    event_id = int(cursor.lastrowid)
    try:
        conn.execute(
            "INSERT INTO raw_events_fts(raw_event_id, content, topic_hint, domain_hint) VALUES(?, ?, ?, ?)",
            (event_id, content, topic_hint, domain_hint),
        )
    except Exception:
        pass
    try:
        from session_archive import record_session_message

        record_session_message(
            root,
            subject_id=subject_id,
            session_id=session_id,
            source_type=source_type,
            content=content,
            raw_event_id=event_id,
            timestamp=event_time,
            profile_id=profile_id,
            workspace_id=workspace_id,
            origin_agent_id=origin_agent_id,
            shared_mode=shared_mode,
            conn=conn,
        )
    except Exception as exc:
        LOGGER.warning("Session archive projection failed for raw_event=%s: %s", event_id, exc)

    return {
        "status": "ok",
        "inserted": True,
        "raw_event_id": event_id,
        "subject_id": subject_id,
        "session_id": session_id,
        "source_type": source_type,
        "content_hash": content_hash,
        "topic_hint": topic_hint,
        "domain_hint": domain_hint,
        "turn_uid": turn_uid,
        "message_role": message_role,
    }


def insert_raw_event(root: Path, **kwargs) -> dict[str, object]:
    """Compatibility wrapper that owns the SQLite transaction."""
    conn = open_db(root)
    try:
        result = insert_raw_event_with_conn(conn, root, **kwargs)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    payload = load_payload(args.payload_file)
    content = read_content(args, payload)
    if not content:
        raise SystemExit("Content is required via --content, --content-file, or --payload-file.")

    subject_id = str(arg_or_payload(args, payload, "subject_id", "person-unknown"))
    subject_name = str(arg_or_payload(args, payload, "subject_name", "Unknown"))
    session_id = str(arg_or_payload(args, payload, "session_id", ""))
    source_type = str(arg_or_payload(args, payload, "source_type", "conversation"))
    source_ref = str(arg_or_payload(args, payload, "source_ref", ""))
    topic_hint = str(arg_or_payload(args, payload, "topic_hint", ""))
    domain_hint = str(arg_or_payload(args, payload, "domain_hint", ""))
    event_time = str(arg_or_payload(args, payload, "event_time", ""))
    allow_duplicate = bool(payload.get("allow_duplicate", False) or args.allow_duplicate)
    profile_id = str(arg_or_payload(args, payload, "profile_id", "default"))
    workspace_id = str(arg_or_payload(args, payload, "workspace_id", "default"))
    origin_agent_id = str(arg_or_payload(args, payload, "agent_id", payload.get("origin_agent_id", "")))
    visibility_scope = str(arg_or_payload(args, payload, "visibility_scope", "workspace"))
    event_uid = str(arg_or_payload(args, payload, "event_uid", ""))
    idempotency_key = str(arg_or_payload(args, payload, "idempotency_key", ""))
    turn_uid = str(arg_or_payload(args, payload, "turn_uid", ""))
    message_role = str(arg_or_payload(args, payload, "message_role", ""))
    sequence_value = arg_or_payload(args, payload, "message_sequence", None)
    try:
        message_sequence = int(sequence_value) if sequence_value not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise SystemExit("message_sequence must be an integer.") from exc

    root = store_root(args.store)
    emit(
        insert_raw_event(
            root,
            subject_id=subject_id,
            subject_name=subject_name,
            session_id=session_id,
            source_type=source_type,
            source_ref=source_ref,
            topic_hint=topic_hint,
            domain_hint=domain_hint,
            event_time=event_time,
            content=content,
            allow_duplicate=allow_duplicate,
            profile_id=profile_id,
            workspace_id=workspace_id,
            origin_agent_id=origin_agent_id, visibility_scope=visibility_scope,
            event_uid=event_uid, idempotency_key=idempotency_key,
            turn_uid=turn_uid, message_role=message_role, message_sequence=message_sequence,
            shared_mode=bool(getattr(args, "shared_mode", False)),
        )
    )


if __name__ == "__main__":
    main()
