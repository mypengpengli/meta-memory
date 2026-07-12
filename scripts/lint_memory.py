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

    migration_rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    if [str(row[0]) for row in migration_rows] != ["001", "002", "003", "004"]:
        issues.append(issue("error", "schema_migrations_incomplete", "The store has not completed all Meta Memory 2.0 migrations."))

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
    claim_backed_paths = {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT memory_path FROM claims WHERE COALESCE(memory_path, '') != '' AND EXISTS (SELECT 1 FROM claim_sources WHERE claim_sources.claim_id = claims.id)"
        ).fetchall()
    }

    canonical_counts: dict[tuple[str, str], int] = Counter()
    long_term_notes: dict[tuple[str, str], list[str]] = defaultdict(list)
    for raw in rows:
        path, subject_id, memory_kind, page_role, canonical, source_count = raw
        key = (str(subject_id or ""), str(memory_kind or ""))
        if int(canonical or 0) == 1:
            canonical_counts[key] += 1
        if str(memory_kind) in LONG_TERM_KINDS and int(source_count or 0) == 0 and str(path) not in claim_backed_paths:
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

    claim_rows = conn.execute(
        """SELECT c.id, c.subject_id, c.valid_from, c.valid_to, c.memory_path, COUNT(cs.raw_event_id)
           FROM claims c LEFT JOIN claim_sources cs ON cs.claim_id=c.id
           GROUP BY c.id, c.subject_id, c.valid_from, c.valid_to, c.memory_path"""
    ).fetchall()
    for claim_id, subject_id, valid_from, valid_to, memory_path, source_count in claim_rows:
        if int(source_count or 0) == 0:
            issues.append(issue("warning", "claim_without_sources", "Claim has no raw-event evidence.", claim_id=claim_id, subject_id=subject_id))
        if valid_from and valid_to and str(valid_to) <= str(valid_from):
            issues.append(issue("error", "invalid_claim_interval", "Claim validity interval ends before it starts.", claim_id=claim_id))
        if memory_path and not Path(str(memory_path)).exists():
            issues.append(issue("warning", "missing_claim_file", "Claim points to a missing Markdown file.", claim_id=claim_id, path=memory_path))

    card_rows = conn.execute("SELECT id, subject_id, source_event_ids FROM session_cards").fetchall()
    for card_id, subject_id, raw_ids in card_rows:
        try:
            ids = [int(value) for value in __import__("json").loads(raw_ids or "[]")]
        except (ValueError, TypeError):
            ids = []
        if not ids:
            issues.append(issue("warning", "empty_session_card", "Session card has no source event IDs.", card_id=card_id, subject_id=subject_id))

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
