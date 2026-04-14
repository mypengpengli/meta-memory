#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, sha256_text, store_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest a raw event into the event inbox without organizing it yet.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--subject-id", default="person-unknown", help="Primary subject id")
    parser.add_argument("--subject-name", default="Unknown", help="Primary subject display name")
    parser.add_argument("--session-id", default="", help="Session id for grouping recent events")
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


def insert_raw_event(
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
) -> dict[str, object]:
    conn = open_db(root)
    content_hash = sha256_text(content)

    duplicate = None
    if not allow_duplicate:
        duplicate = conn.execute(
            """
            SELECT id, created_at, processed_state
            FROM raw_events
            WHERE subject_id = ?
              AND content_hash = ?
              AND COALESCE(session_id, '') = ?
              AND COALESCE(source_ref, '') = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (subject_id, content_hash, session_id, source_ref),
        ).fetchone()

    if duplicate:
        conn.close()
        return {
            "status": "ok",
            "inserted": False,
            "duplicate_of": {
                "id": duplicate[0],
                "created_at": duplicate[1],
                "processed_state": duplicate[2],
            },
            "subject_id": subject_id,
            "content_hash": content_hash,
        }

    cursor = conn.execute(
        """
        INSERT INTO raw_events(
            subject_id, subject_name, session_id, source_type, source_ref,
            content, content_hash, topic_hint, domain_hint, event_time, processed_state
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
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
        ),
    )
    conn.commit()
    event_id = int(cursor.lastrowid)
    conn.close()

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
    }


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
        )
    )


if __name__ == "__main__":
    main()
