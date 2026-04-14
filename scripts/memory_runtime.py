#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from classify_memory import classify
from ingest_memory import build_payload, load_payload as load_memory_payload, read_input
from ingest_raw_event import insert_raw_event
from write_memory import write_payload
from _common import emit, ensure_store_ready, open_db, store_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Host-facing runtime bridge for recording events, preparing context, and explicit remember actions."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record-event", help="Record an arbitrary raw event into the inbox")
    add_shared_record_args(record)
    record.add_argument("--allow-duplicate", action="store_true", help="Allow exact duplicate raw events")

    prepare = subparsers.add_parser("prepare-context", help="Record the current user turn, run heartbeat, and retrieve memory context")
    prepare.add_argument("--store", required=True, help="Path to the external memory-data root")
    prepare.add_argument("--subject-id", default="person-unknown", help="Primary subject id")
    prepare.add_argument("--subject-name", default="Unknown", help="Primary subject display name")
    prepare.add_argument("--session-id", default="", help="Session id")
    prepare.add_argument("--query", help="Current user query")
    prepare.add_argument("--query-file", help="Read the current user query from a UTF-8 text file")
    prepare.add_argument("--topic-hint", default="", help="Optional topic hint for the raw event")
    prepare.add_argument("--domain-hint", default="", help="Optional domain hint for the raw event")
    prepare.add_argument("--source-ref", default="", help="Optional raw event source reference")
    prepare.add_argument("--event-time", default="", help="Optional ISO-like event time")
    prepare.add_argument("--skip-record-query", action="store_true", help="Do not append the current query to raw_events")
    prepare.add_argument("--allow-duplicate", action="store_true", help="Allow exact duplicate raw events when recording the query")
    prepare.add_argument("--skip-heartbeat", action="store_true", help="Do not run heartbeat before retrieval")
    prepare.add_argument("--heartbeat-policy", choices=["conservative", "balanced", "aggressive"], default="balanced")
    prepare.add_argument("--heartbeat-interval-minutes", type=int, default=30)
    prepare.add_argument("--heartbeat-min-pending", type=int, default=3)
    prepare.add_argument("--heartbeat-max-events", type=int, default=20)
    prepare.add_argument("--top-k", type=int, default=6, help="Maximum retrieved memories")
    prepare.add_argument("--include-candidates", action="store_true", help="Allow candidate memories in retrieval")
    prepare.add_argument("--no-basics", action="store_true", help="Do not force-include profile/state memories")
    prepare.add_argument("--raw-limit", type=int, default=3, help="Maximum raw evidence snippets to include")
    prepare.add_argument("--skip-raw-evidence", action="store_true", help="Do not search raw events for evidence snippets")
    prepare.add_argument("--context-out-file", help="Write the rendered prompt context to a UTF-8 text file")
    prepare.add_argument("--out-file", help="Write the full JSON result to a UTF-8 file")

    finalize = subparsers.add_parser("finalize-turn", help="Record the assistant reply and optionally organize the finished turn")
    finalize.add_argument("--store", required=True, help="Path to the external memory-data root")
    finalize.add_argument("--subject-id", default="person-unknown", help="Primary subject id")
    finalize.add_argument("--subject-name", default="Unknown", help="Primary subject display name")
    finalize.add_argument("--session-id", default="", help="Session id")
    finalize.add_argument("--reply", help="Assistant reply text")
    finalize.add_argument("--reply-file", help="Read the assistant reply from a UTF-8 text file")
    finalize.add_argument("--topic-hint", default="", help="Optional topic hint")
    finalize.add_argument("--domain-hint", default="", help="Optional domain hint")
    finalize.add_argument("--source-ref", default="", help="Optional source reference")
    finalize.add_argument("--event-time", default="", help="Optional ISO-like event time")
    finalize.add_argument("--allow-duplicate", action="store_true", help="Allow exact duplicate raw events")
    finalize.add_argument("--skip-record-reply", action="store_true", help="Do not record the assistant reply before heartbeat")
    finalize.add_argument("--skip-heartbeat", action="store_true", help="Do not run heartbeat after recording the reply")
    finalize.add_argument("--heartbeat-policy", choices=["conservative", "balanced", "aggressive"], default="balanced")
    finalize.add_argument("--heartbeat-interval-minutes", type=int, default=30)
    finalize.add_argument("--heartbeat-min-pending", type=int, default=2)
    finalize.add_argument("--heartbeat-max-events", type=int, default=20)
    finalize.add_argument("--out-file", help="Write the full JSON result to a UTF-8 file")

    remember = subparsers.add_parser("remember", help="Explicitly write a memory while also recording its raw source")
    remember.add_argument("--store", required=True, help="Path to the external memory-data root")
    remember.add_argument("--subject-id", default="person-unknown", help="Primary subject id")
    remember.add_argument("--subject-name", default="Unknown", help="Primary subject display name")
    remember.add_argument("--session-id", default="", help="Session id")
    remember.add_argument("--title", help="Memory title")
    remember.add_argument("--content", help="Memory content")
    remember.add_argument("--content-file", help="Read content from a UTF-8 text file")
    remember.add_argument("--payload-file", help="Read title/content/metadata from a UTF-8 JSON file")
    remember.add_argument(
        "--force-kind",
        choices=["profile", "state", "event", "relationship", "goal", "domain", "session", "candidate"],
        help="Override the classifier result",
    )
    remember.add_argument(
        "--use-underlying-kind",
        action="store_true",
        help="If the classifier recommends session/candidate, write to the suggested long-term kind instead",
    )
    remember.add_argument("--domain", help="Override domain")
    remember.add_argument("--topic", help="Override topic")
    remember.add_argument("--source", help="Override source")
    remember.add_argument("--start-at", help="Override start time")
    remember.add_argument("--end-at", help="Override end time")
    remember.add_argument("--confidence", type=float, help="Override confidence")
    remember.add_argument("--status", help="Override status")
    remember.add_argument("--tag", action="append", default=[], help="Additional tag; may be repeated")
    remember.add_argument("--related-person", action="append", default=[], help="Related person; may be repeated")
    remember.add_argument("--related-event", action="append", default=[], help="Related event; may be repeated")
    remember.add_argument("--related-source", action="append", default=[], help="Related source; may be repeated")
    remember.add_argument("--slug", help="Override slug")
    remember.add_argument("--mode", choices=["create", "replace", "append"], default="create")
    remember.add_argument("--topic-hint", default="", help="Optional topic hint for the raw event")
    remember.add_argument("--domain-hint", default="", help="Optional domain hint for the raw event")
    remember.add_argument("--source-ref", default="", help="Optional raw event source reference")
    remember.add_argument("--event-time", default="", help="Optional ISO-like event time")
    remember.add_argument("--skip-raw-record", action="store_true", help="Do not append a raw event before writing the memory")
    remember.add_argument("--allow-duplicate", action="store_true", help="Allow exact duplicate raw events")
    remember.add_argument("--skip-index", action="store_true", help="Do not reindex/rescore after writing")
    remember.add_argument("--out-file", help="Write the full JSON result to a UTF-8 file")

    return parser.parse_args()


def add_shared_record_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", required=True, help="Path to the external memory-data root")
    parser.add_argument("--subject-id", default="person-unknown", help="Primary subject id")
    parser.add_argument("--subject-name", default="Unknown", help="Primary subject display name")
    parser.add_argument("--session-id", default="", help="Session id")
    parser.add_argument("--source-type", default="conversation", help="Raw event source type")
    parser.add_argument("--source-ref", default="", help="Optional raw event source reference")
    parser.add_argument("--topic-hint", default="", help="Optional topic hint")
    parser.add_argument("--domain-hint", default="", help="Optional domain hint")
    parser.add_argument("--event-time", default="", help="Optional ISO-like event time")
    parser.add_argument("--content", help="Inline raw content")
    parser.add_argument("--content-file", help="Read raw content from a UTF-8 text file")
    parser.add_argument("--payload-file", help="Read a UTF-8 JSON payload file")


def read_text_arg(value: str | None, path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8-sig").strip()
    return (value or "").strip()


def write_json_file(path: str | None, payload: dict[str, object]) -> None:
    if not path:
        return
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_json_script(script_name: str, *args: str) -> dict[str, object]:
    base = Path(__file__).resolve().parent
    result = subprocess.run(
        [sys.executable, str(base / script_name), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def format_memory_context(retrieved: dict[str, object], raw_evidence: dict[str, object] | None) -> str:
    evidence_rows = list((raw_evidence or {}).get("results", []))
    positive_evidence = [item for item in evidence_rows if float(item.get("score", 0.0) or 0.0) > 0.0]
    if positive_evidence:
        evidence_rows = positive_evidence

    selected = list(retrieved.get("selected", []))
    relevant_selected = [item for item in selected if float(item.get("query_score", 0.0) or 0.0) > 0.0]
    fallback_selected = [item for item in selected if item not in relevant_selected]

    lines = [
        "# Memory Context",
        "",
        "Use these memories only when they are relevant to the current turn.",
        "Prefer current facts and direct answers over irrelevant historical detail.",
        "",
        "## Retrieved Memories",
    ]

    if relevant_selected:
        display_selected = relevant_selected
    elif evidence_rows:
        display_selected = []
    else:
        display_selected = fallback_selected[:2]

    if not display_selected:
        lines.append("- No relevant structured memories were found.")
    else:
        for item in display_selected:
            summary = str(item.get("summary", "")).strip() or str(item.get("topic", "")).strip() or str(item.get("title", "")).strip()
            lines.append(
                f"- [{item.get('memory_kind', 'note')}] {item.get('title', '')} | {item.get('domain', '')} / {item.get('topic', '')} | {summary}"
            )

    lines.extend(["", "## Raw Evidence"])
    if not evidence_rows:
        lines.append("- No additional raw evidence was selected.")
    else:
        for item in evidence_rows:
            lines.append(
                f"- {item.get('effective_time', '')} | {item.get('domain_hint', '')} / {item.get('topic_hint', '')} | {item.get('snippet', '')}"
            )

    return "\n".join(lines).strip() + "\n"


def link_memory_source(conn, memory_path: str, raw_event_id: int, link_role: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO memory_sources(memory_path, raw_event_id, link_role)
        VALUES(?, ?, ?)
        """,
        (memory_path, raw_event_id, link_role),
    )


def set_raw_event_state(root: Path, raw_event_id: int, state: str, note: dict[str, object] | None = None) -> None:
    conn = open_db(root)
    conn.execute(
        """
        UPDATE raw_events
        SET
            processed_state = ?,
            note = COALESCE(?, note)
        WHERE id = ?
        """,
        (
            state,
            json.dumps(note, ensure_ascii=False) if note is not None else None,
            raw_event_id,
        ),
    )
    conn.commit()
    conn.close()


def mark_raw_event_organized(
    root: Path,
    raw_event_id: int,
    *,
    classifier_kind: str,
    classifier_domain: str,
    target_memory_kind: str,
    target_memory_path: str,
    note: dict[str, object],
    link_role: str,
) -> None:
    conn = open_db(root)
    conn.execute(
        """
        UPDATE raw_events
        SET
            processed_state = 'organized',
            processed_at = CURRENT_TIMESTAMP,
            classifier_kind = ?,
            classifier_domain = ?,
            target_memory_kind = ?,
            target_memory_path = ?,
            note = ?
        WHERE id = ?
        """,
        (
            classifier_kind,
            classifier_domain,
            target_memory_kind,
            target_memory_path,
            json.dumps(note, ensure_ascii=False),
            raw_event_id,
        ),
    )
    link_memory_source(conn, target_memory_path, raw_event_id, link_role)
    conn.commit()
    conn.close()


def prepare_context(args: argparse.Namespace) -> dict[str, object]:
    root = store_root(args.store)
    bootstrap = ensure_store_ready(root)
    query = read_text_arg(args.query, args.query_file)
    if not query:
        raise SystemExit("Query is required via --query or --query-file.")

    heartbeat = None
    if not args.skip_heartbeat:
        heartbeat_args = [
            "--store",
            str(root),
            "--subject-id",
            args.subject_id,
            "--policy",
            args.heartbeat_policy,
            "--interval-minutes",
            str(args.heartbeat_interval_minutes),
            "--min-pending",
            str(args.heartbeat_min_pending),
            "--max-events",
            str(args.heartbeat_max_events),
        ]
        heartbeat = run_json_script("run_heartbeat.py", *heartbeat_args)

    retrieve_args = [
        "--store",
        str(root),
        "--subject-id",
        args.subject_id,
        "--query",
        query,
        "--top-k",
        str(args.top_k),
    ]
    if args.include_candidates:
        retrieve_args.append("--include-candidates")
    if args.no_basics:
        retrieve_args.append("--no-basics")
    retrieved = run_json_script("retrieve_memories.py", *retrieve_args)

    raw_evidence = None
    if not args.skip_raw_evidence and args.raw_limit > 0:
        raw_args = [
            "--store",
            str(root),
            "--subject-id",
            args.subject_id,
            "--query",
            query,
            "--processed-state",
            "organized",
            "--limit",
            str(args.raw_limit),
        ]
        raw_evidence = run_json_script("search_raw_events.py", *raw_args)

    recorded = None
    if not args.skip_record_query:
        recorded = insert_raw_event(
            root,
            subject_id=args.subject_id,
            subject_name=args.subject_name,
            session_id=args.session_id,
            source_type="conversation-user",
            source_ref=args.source_ref,
            topic_hint=args.topic_hint,
            domain_hint=args.domain_hint,
            event_time=args.event_time,
            content=query,
            allow_duplicate=args.allow_duplicate,
        )

    context = format_memory_context(retrieved, raw_evidence)
    if args.context_out_file:
        Path(args.context_out_file).write_text(context, encoding="utf-8")

    result = {
        "status": "ok",
        "command": "prepare-context",
        "store_bootstrap": bootstrap,
        "query": query,
        "recorded_raw_event": recorded,
        "heartbeat": heartbeat,
        "retrieved": retrieved,
        "raw_evidence": raw_evidence,
        "context_markdown": context,
    }
    write_json_file(args.out_file, result)
    return result


def remember_memory(args: argparse.Namespace) -> dict[str, object]:
    root = store_root(args.store)
    bootstrap = ensure_store_ready(root)
    payload = load_memory_payload(args.payload_file)
    title, content = read_input(args, payload)
    subject_id = str(payload.get("subject_id", args.subject_id))
    subject_name = str(payload.get("subject_name", args.subject_name))

    raw_record = None
    if not args.skip_raw_record:
        raw_record = insert_raw_event(
            root,
            subject_id=subject_id,
            subject_name=subject_name,
            session_id=args.session_id,
            source_type="explicit-memory",
            source_ref=args.source_ref,
            topic_hint=args.topic_hint or args.topic or "",
            domain_hint=args.domain_hint or args.domain or "",
            event_time=args.event_time,
            content=content,
            allow_duplicate=args.allow_duplicate,
        )
        if raw_record.get("inserted"):
            set_raw_event_state(
                root,
                int(raw_record["raw_event_id"]),
                "in_progress",
                {"origin": "explicit-remember", "stage": "writing"},
            )

    classification = classify(title, content, subject_id, subject_name)
    final_payload = build_payload(classification, payload, args, title, content)

    if raw_record and raw_record.get("inserted"):
        raw_source = f"raw_event:{raw_record['raw_event_id']}"
        final_payload["source"] = raw_source
        related_sources = list(final_payload.get("related_sources", []))
        if raw_source not in related_sources:
            related_sources.append(raw_source)
        final_payload["related_sources"] = related_sources

    try:
        written = write_payload(root, final_payload, skip_index=args.skip_index)
    except Exception:
        if raw_record and raw_record.get("inserted"):
            set_raw_event_state(
                root,
                int(raw_record["raw_event_id"]),
                "pending",
                {"origin": "explicit-remember", "stage": "write_failed"},
            )
        raise

    if raw_record and raw_record.get("inserted"):
        mark_raw_event_organized(
            root,
            int(raw_record["raw_event_id"]),
            classifier_kind=str(classification["recommended_kind"]),
            classifier_domain=str(classification["recommended_domain"]),
            target_memory_kind=str(final_payload["kind"]),
            target_memory_path=str(written["path"]),
            note={
                "origin": "explicit-remember",
                "recommended_kind": classification["recommended_kind"],
                "underlying_long_term_kind": classification["underlying_long_term_kind"],
                "classification_confidence": classification["classification_confidence"],
                "final_kind": final_payload["kind"],
            },
            link_role="explicit-remember",
        )

    result = {
        "status": "ok",
        "command": "remember",
        "store_bootstrap": bootstrap,
        "classification": classification,
        "final_payload": final_payload,
        "raw_event": raw_record,
        "written": written,
    }
    write_json_file(args.out_file, result)
    return result


def record_event(args: argparse.Namespace) -> dict[str, object]:
    payload = load_memory_payload(args.payload_file)
    content = read_text_arg(args.content, args.content_file) or str(payload.get("content", "")).strip()
    if not content:
        raise SystemExit("Content is required via --content, --content-file, or --payload-file.")
    root = store_root(args.store)
    bootstrap = ensure_store_ready(root)
    result = insert_raw_event(
        root,
        subject_id=str(payload.get("subject_id", args.subject_id)),
        subject_name=str(payload.get("subject_name", args.subject_name)),
        session_id=str(payload.get("session_id", args.session_id)),
        source_type=str(payload.get("source_type", args.source_type)),
        source_ref=str(payload.get("source_ref", args.source_ref)),
        topic_hint=str(payload.get("topic_hint", args.topic_hint)),
        domain_hint=str(payload.get("domain_hint", args.domain_hint)),
        event_time=str(payload.get("event_time", args.event_time)),
        content=content,
        allow_duplicate=bool(payload.get("allow_duplicate", False) or args.allow_duplicate),
    )
    return {"status": "ok", "command": "record-event", "store_bootstrap": bootstrap, "result": result}


def finalize_turn(args: argparse.Namespace) -> dict[str, object]:
    root = store_root(args.store)
    bootstrap = ensure_store_ready(root)
    reply = read_text_arg(args.reply, args.reply_file)
    if not reply and not args.skip_record_reply:
        raise SystemExit("Reply is required via --reply or --reply-file unless --skip-record-reply is set.")

    recorded = None
    if not args.skip_record_reply:
        recorded = insert_raw_event(
            root,
            subject_id=args.subject_id,
            subject_name=args.subject_name,
            session_id=args.session_id,
            source_type="conversation-assistant",
            source_ref=args.source_ref,
            topic_hint=args.topic_hint,
            domain_hint=args.domain_hint,
            event_time=args.event_time,
            content=reply,
            allow_duplicate=args.allow_duplicate,
        )

    heartbeat = None
    if not args.skip_heartbeat:
        heartbeat_args = [
            "--store",
            str(root),
            "--subject-id",
            args.subject_id,
            "--policy",
            args.heartbeat_policy,
            "--interval-minutes",
            str(args.heartbeat_interval_minutes),
            "--min-pending",
            str(args.heartbeat_min_pending),
            "--max-events",
            str(args.heartbeat_max_events),
        ]
        heartbeat = run_json_script("run_heartbeat.py", *heartbeat_args)

    result = {
        "status": "ok",
        "command": "finalize-turn",
        "store_bootstrap": bootstrap,
        "recorded_raw_event": recorded,
        "heartbeat": heartbeat,
    }
    write_json_file(args.out_file, result)
    return result


def main() -> None:
    args = parse_args()
    if args.command == "prepare-context":
        emit(prepare_context(args))
        return
    if args.command == "remember":
        emit(remember_memory(args))
        return
    if args.command == "record-event":
        emit(record_event(args))
        return
    if args.command == "finalize-turn":
        emit(finalize_turn(args))
        return
    raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
