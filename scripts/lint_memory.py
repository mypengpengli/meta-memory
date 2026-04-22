#!/usr/bin/env python3
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from classify_memory import LONG_TERM_KINDS
from _common import DEFAULT_STORE_HELP, emit, open_db, store_root


def parse_args():
    import argparse

    parser = argparse.ArgumentParser(description="Lint the memory store for structural and safety issues.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--pending-age-hours", type=float, default=24.0, help="Warn when pending raw events are older than this")
    return parser.parse_args()


def issue(severity: str, code: str, message: str, **details: object) -> dict[str, object]:
    payload = {"severity": severity, "code": code, "message": message}
    payload.update(details)
    return payload


def parse_created_at(raw: str) -> datetime | None:
    text = str(raw or "").strip()
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


def main() -> None:
    args = parse_args()
    root = store_root(args.store)
    conn = open_db(root)
    issues: list[dict[str, object]] = []

    for filename in ("index.md", "log.md", "sources.md"):
        path = root / filename
        if not path.exists():
            issues.append(issue("warning", "missing_view", f"Missing generated view `{filename}`.", path=str(path)))

    rows = conn.execute(
        """
        SELECT
            d.path,
            d.subject_id,
            d.memory_kind,
            d.page_role,
            d.canonical,
            COUNT(ms.id) AS source_count
        FROM documents AS d
        LEFT JOIN memory_sources AS ms ON ms.memory_path = d.path
        GROUP BY d.path, d.subject_id, d.memory_kind, d.page_role, d.canonical
        """
    ).fetchall()

    canonical_counts: dict[tuple[str, str], int] = Counter()
    long_term_notes: dict[tuple[str, str], list[str]] = defaultdict(list)
    for raw in rows:
        path, subject_id, memory_kind, page_role, canonical, source_count = raw
        key = (str(subject_id or ""), str(memory_kind or ""))
        if int(canonical or 0) == 1:
            canonical_counts[key] += 1
        if str(memory_kind) in LONG_TERM_KINDS and int(source_count or 0) == 0:
            issues.append(
                issue(
                    "warning",
                    "long_term_without_sources",
                    "Long-term memory page has no linked raw source.",
                    path=str(path),
                    memory_kind=str(memory_kind),
                    subject_id=str(subject_id or ""),
                )
            )
        if str(memory_kind) in LONG_TERM_KINDS and int(canonical or 0) == 0:
            long_term_notes[key].append(str(path))
        if str(page_role) in {"session-current", "candidate-pool"} and int(canonical or 0) != 1:
            issues.append(
                issue(
                    "warning",
                    "volatile_page_not_canonical",
                    "Session/candidate current pages should be marked canonical.",
                    path=str(path),
                    page_role=str(page_role),
                )
            )

    for (subject_id, memory_kind), count in sorted(canonical_counts.items()):
        if count > 1:
            issues.append(
                issue(
                    "warning",
                    "multiple_canonical_pages",
                    "Multiple canonical pages exist for the same subject and kind.",
                    subject_id=subject_id,
                    memory_kind=memory_kind,
                    count=count,
                )
            )

    for (subject_id, memory_kind), paths in sorted(long_term_notes.items()):
        if len(paths) > 5:
            issues.append(
                issue(
                    "info",
                    "many_long_term_notes",
                    "This subject/kind has many non-canonical long-term notes; consider consolidation.",
                    subject_id=subject_id,
                    memory_kind=memory_kind,
                    count=len(paths),
                )
            )

    auto_rows = conn.execute(
        """
        SELECT DISTINCT
            d.path,
            d.subject_id,
            d.memory_kind,
            r.source_type
        FROM documents AS d
        JOIN memory_sources AS ms ON ms.memory_path = d.path
        JOIN raw_events AS r ON r.id = ms.raw_event_id
        WHERE
            ms.link_role = 'auto-organized'
            AND d.memory_kind IN ('profile', 'state', 'event', 'relationship', 'goal', 'domain')
            AND r.source_type IN ('conversation-user', 'conversation-assistant')
        """
    ).fetchall()
    for path, subject_id, memory_kind, source_type in auto_rows:
        issues.append(
            issue(
                "error",
                "conversation_promoted_to_long_term",
                "Conversation turns should not be auto-organized directly into long-term memory.",
                path=str(path),
                subject_id=str(subject_id or ""),
                memory_kind=str(memory_kind),
                source_type=str(source_type),
            )
        )

    pending_rows = conn.execute(
        """
        SELECT id, subject_id, source_type, created_at
        FROM raw_events
        WHERE processed_state = 'pending'
        ORDER BY id ASC
        """
    ).fetchall()
    now = datetime.now(timezone.utc)
    for raw_event_id, subject_id, source_type, created_at in pending_rows:
        created = parse_created_at(str(created_at or ""))
        age_hours = None
        if created is not None:
            age_hours = round((now - created).total_seconds() / 3600.0, 2)
        if age_hours is not None and age_hours >= args.pending_age_hours:
            issues.append(
                issue(
                    "warning",
                    "stale_pending_raw_event",
                    "Raw event has been pending for too long.",
                    raw_event_id=int(raw_event_id),
                    subject_id=str(subject_id or ""),
                    source_type=str(source_type or ""),
                    age_hours=age_hours,
                )
            )

    conn.close()
    emit(
        {
            "status": "ok",
            "store": str(root),
            "issue_count": len(issues),
            "issues": issues,
        }
    )


if __name__ == "__main__":
    main()
