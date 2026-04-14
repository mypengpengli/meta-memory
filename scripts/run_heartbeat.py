#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from classify_memory import LONG_TERM_KINDS, classify, first_sentence, slugify
from write_memory import DEFAULT_CONFIDENCE, DEFAULT_STATUS, run_indexing, write_payload
from _common import emit, open_db, store_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the lightweight heartbeat and incrementally organize new raw events."
    )
    parser.add_argument("--store", required=True, help="Path to the external memory-data root")
    parser.add_argument("--subject-id", help="Limit to one subject_id")
    parser.add_argument("--interval-minutes", type=int, default=30, help="Minimum interval between organize runs")
    parser.add_argument("--min-pending", type=int, default=3, help="Pending event threshold that triggers organize")
    parser.add_argument("--max-events", type=int, default=20, help="Maximum raw events to organize per subject")
    parser.add_argument(
        "--policy",
        choices=["conservative", "balanced", "aggressive"],
        default="balanced",
        help="How aggressively to write directly into long-term layers",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write or update any state")
    parser.add_argument("--skip-index", action="store_true", help="Skip final reindex/rescore pass")
    return parser.parse_args()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_db_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    normalized = text.replace(" ", "T")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def ensure_cursor(conn, subject_id: str, heartbeat_at: str, organized_at: str | None = None, last_processed_event_id: int | None = None) -> None:
    if organized_at is None and last_processed_event_id is None:
        conn.execute(
            """
            INSERT INTO maintenance_cursor(subject_id, last_heartbeat_at)
            VALUES(?, ?)
            ON CONFLICT(subject_id) DO UPDATE SET
                last_heartbeat_at=excluded.last_heartbeat_at
            """,
            (subject_id, heartbeat_at),
        )
        return

    conn.execute(
        """
        INSERT INTO maintenance_cursor(subject_id, last_processed_event_id, last_organized_at, last_heartbeat_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(subject_id) DO UPDATE SET
            last_processed_event_id=excluded.last_processed_event_id,
            last_organized_at=excluded.last_organized_at,
            last_heartbeat_at=excluded.last_heartbeat_at
        """,
        (
            subject_id,
            last_processed_event_id or 0,
            organized_at or heartbeat_at,
            heartbeat_at,
        ),
    )


def should_organize(pending_count: int, last_organized_at: datetime | None, interval_minutes: int, min_pending: int) -> tuple[bool, str]:
    if pending_count <= 0:
        return False, "no_pending_events"
    if last_organized_at is None:
        return True, "never_organized"
    if pending_count >= min_pending:
        return True, "pending_threshold_reached"
    if utc_now() - last_organized_at >= timedelta(minutes=interval_minutes):
        return True, "interval_elapsed"
    return False, "waiting_for_threshold_or_interval"


def choose_target_kind(classification: dict[str, object], policy: str) -> str:
    recommended = str(classification["recommended_kind"])
    underlying = str(classification["underlying_long_term_kind"])
    confidence = float(classification["classification_confidence"])

    if policy == "conservative":
        if recommended == "session":
            return "session"
        return "candidate"

    if policy == "aggressive":
        if recommended in LONG_TERM_KINDS:
            return recommended
        if underlying in LONG_TERM_KINDS:
            return underlying
        return recommended

    if recommended in {"session", "candidate"}:
        return recommended
    if recommended in LONG_TERM_KINDS and confidence >= 0.85:
        return recommended
    return "candidate"


def build_payload_from_event(event: dict[str, object], classification: dict[str, object], policy: str) -> dict[str, object]:
    target_kind = choose_target_kind(classification, policy)
    suggested = dict(classification["suggested_payload"])
    underlying = str(classification["underlying_long_term_kind"])
    tags = list(suggested.get("tags", []))
    for tag in ["auto-organized", event["source_type"]]:
        if tag and tag not in tags:
            tags.append(tag)
    if target_kind == "candidate" and underlying in LONG_TERM_KINDS:
        hint = f"suggested-{underlying}"
        if hint not in tags:
            tags.append(hint)

    confidence = float(suggested.get("confidence", 0.35))
    status = str(suggested.get("status", "pending"))
    if target_kind in LONG_TERM_KINDS and target_kind != str(suggested.get("kind", "")):
        confidence = max(confidence, DEFAULT_CONFIDENCE[target_kind])
        status = DEFAULT_STATUS[target_kind]

    return {
        "title": event["title"],
        "content": event["content"],
        "kind": target_kind,
        "subject_id": event["subject_id"],
        "subject_name": event["subject_name"],
        "domain": event["domain_hint"] or suggested.get("domain", "general"),
        "topic": event["topic_hint"] or suggested.get("topic", slugify(event["title"])),
        "tags": tags,
        "status": status,
        "confidence": confidence,
        "source": f"raw_event:{event['id']}",
        "related_sources": [f"raw_event:{event['id']}"],
        "slug": f"raw-{event['id']}-{slugify(event['title'])}",
        "mode": "create",
        "start_at": event["event_time"] or "",
        "related_people": [],
        "related_events": [],
        "supersedes": [],
        "replaced_by": [],
    }


def note_json(classification: dict[str, object], payload: dict[str, object], policy: str) -> str:
    compact = {
        "policy": policy,
        "recommended_kind": classification["recommended_kind"],
        "underlying_long_term_kind": classification["underlying_long_term_kind"],
        "classification_confidence": classification["classification_confidence"],
        "target_kind": payload["kind"],
    }
    return json.dumps(compact, ensure_ascii=False)


def link_memory_source(conn, memory_path: str, raw_event_id: int, link_role: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO memory_sources(memory_path, raw_event_id, link_role)
        VALUES(?, ?, ?)
        """,
        (memory_path, raw_event_id, link_role),
    )


def process_subject(conn, root: Path, subject_id: str, max_events: int, policy: str, dry_run: bool) -> dict[str, object]:
    rows = conn.execute(
        """
        SELECT
            id, subject_id, subject_name, session_id, source_type, source_ref,
            content, content_hash, topic_hint, domain_hint, event_time, created_at
        FROM raw_events
        WHERE subject_id = ? AND processed_state = 'pending'
        ORDER BY id ASC
        LIMIT ?
        """,
        (subject_id, max_events),
    ).fetchall()

    processed: list[dict[str, object]] = []
    batch_id = f"heartbeat-{utc_now().strftime('%Y%m%d-%H%M%S')}-{subject_id or 'unknown'}"

    for raw in rows:
        event = {
            "id": int(raw[0]),
            "subject_id": str(raw[1] or ""),
            "subject_name": str(raw[2] or "Unknown"),
            "session_id": str(raw[3] or ""),
            "source_type": str(raw[4] or "conversation"),
            "source_ref": str(raw[5] or ""),
            "content": str(raw[6] or ""),
            "content_hash": str(raw[7] or ""),
            "topic_hint": str(raw[8] or ""),
            "domain_hint": str(raw[9] or ""),
            "event_time": str(raw[10] or ""),
            "created_at": str(raw[11] or ""),
        }
        title = event["topic_hint"] or first_sentence(event["content"])[:60] or f"raw-event-{event['id']}"
        event["title"] = title
        classification = classify(title, event["content"], event["subject_id"], event["subject_name"])
        payload = build_payload_from_event(event, classification, policy)

        if dry_run:
            processed.append(
                {
                    "raw_event_id": event["id"],
                    "title": title,
                    "recommended_kind": classification["recommended_kind"],
                    "target_kind": payload["kind"],
                    "path": None,
                }
            )
            continue

        written = write_payload(root, payload, skip_index=True)
        memory_path = str(written["path"])
        link_memory_source(conn, memory_path, event["id"], "auto-organized")
        conn.execute(
            """
            UPDATE raw_events
            SET
                processed_state = 'organized',
                processed_at = ?,
                batch_id = ?,
                classifier_kind = ?,
                classifier_domain = ?,
                target_memory_kind = ?,
                target_memory_path = ?,
                note = ?
            WHERE id = ?
            """,
            (
                iso_now(),
                batch_id,
                str(classification["recommended_kind"]),
                str(classification["recommended_domain"]),
                str(payload["kind"]),
                memory_path,
                note_json(classification, payload, policy),
                event["id"],
            ),
        )
        processed.append(
            {
                "raw_event_id": event["id"],
                "title": title,
                "recommended_kind": classification["recommended_kind"],
                "target_kind": payload["kind"],
                "path": memory_path,
            }
        )

    return {
        "subject_id": subject_id,
        "batch_id": batch_id,
        "processed": processed,
    }


def main() -> None:
    args = parse_args()
    root = store_root(args.store)
    conn = open_db(root)

    clauses = ["r.processed_state = 'pending'"]
    params: list[object] = []
    if args.subject_id:
        clauses.append("r.subject_id = ?")
        params.append(args.subject_id)

    subjects = conn.execute(
        f"""
        SELECT
            r.subject_id,
            MAX(r.subject_name) AS subject_name,
            COUNT(*) AS pending_count,
            MIN(r.id) AS first_pending_id,
            MIN(r.created_at) AS oldest_pending_at,
            c.last_processed_event_id,
            c.last_organized_at,
            c.last_heartbeat_at
        FROM raw_events AS r
        LEFT JOIN maintenance_cursor AS c ON c.subject_id = r.subject_id
        WHERE {' AND '.join(clauses)}
        GROUP BY r.subject_id
        ORDER BY MIN(r.id)
        """,
        tuple(params),
    ).fetchall()

    now_text = iso_now()
    wrote_any = False
    summaries: list[dict[str, object]] = []

    if not subjects and args.subject_id:
        if not args.dry_run:
            ensure_cursor(conn, args.subject_id, now_text)
            conn.commit()
        conn.close()
        emit(
            {
                "status": "ok",
                "policy": args.policy,
                "dry_run": args.dry_run,
                "subjects": [
                    {
                        "subject_id": args.subject_id,
                        "pending_count": 0,
                        "organize": False,
                        "reason": "no_pending_events",
                    }
                ],
                "indexed": False,
                "steps": [],
            }
        )
        return

    for row in subjects:
        subject_id = str(row[0] or "")
        pending_count = int(row[2] or 0)
        last_organized_at = parse_db_time(row[6])
        organize, reason = should_organize(
            pending_count,
            last_organized_at,
            args.interval_minutes,
            args.min_pending,
        )

        if not organize:
            if not args.dry_run:
                ensure_cursor(conn, subject_id, now_text)
            summaries.append(
                {
                    "subject_id": subject_id,
                    "pending_count": pending_count,
                    "organize": False,
                    "reason": reason,
                }
            )
            continue

        subject_result = process_subject(conn, root, subject_id, args.max_events, args.policy, args.dry_run)
        processed = subject_result["processed"]
        last_processed_event_id = max((item["raw_event_id"] for item in processed), default=int(row[5] or 0))
        if not args.dry_run:
            ensure_cursor(conn, subject_id, now_text, organized_at=now_text, last_processed_event_id=last_processed_event_id)
            wrote_any = wrote_any or any(item["path"] for item in processed)
        summaries.append(
            {
                "subject_id": subject_id,
                "pending_count": pending_count,
                "organize": True,
                "reason": reason,
                "processed_count": len(processed),
                "processed": processed,
            }
        )

    if not args.dry_run:
        conn.commit()
    conn.close()

    steps: list[dict[str, object]] = []
    if wrote_any and not args.skip_index and not args.dry_run:
        steps = run_indexing(root)

    emit(
        {
            "status": "ok",
            "policy": args.policy,
            "dry_run": args.dry_run,
            "subjects": summaries,
            "indexed": bool(steps),
            "steps": steps,
        }
    )


if __name__ == "__main__":
    main()
