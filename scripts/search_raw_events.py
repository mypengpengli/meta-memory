#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone

from _common import emit, open_db, store_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search raw events by subject, topic, domain, time range, and free-text query."
    )
    parser.add_argument("--store", required=True, help="Path to the external memory-data root")
    parser.add_argument("--subject-id", help="Filter by subject_id")
    parser.add_argument("--session-id", help="Filter by session_id")
    parser.add_argument("--query", help="Free-text query against topic/domain/content")
    parser.add_argument("--query-file", help="Read the free-text query from a UTF-8 text file")
    parser.add_argument("--topic", action="append", default=[], help="Topic hint filter; may be repeated")
    parser.add_argument("--domain", action="append", default=[], help="Domain hint filter; may be repeated")
    parser.add_argument("--source-type", action="append", default=[], help="Source type filter; may be repeated")
    parser.add_argument(
        "--processed-state",
        action="append",
        default=[],
        help="Filter by processed_state such as pending or organized; may be repeated",
    )
    parser.add_argument("--since", help="Only include events at or after this ISO-like time")
    parser.add_argument("--until", help="Only include events at or before this ISO-like time")
    parser.add_argument("--limit", type=int, default=20, help="Maximum results to return")
    parser.add_argument("--full-content", action="store_true", help="Return the full raw content")
    return parser.parse_args()


def read_query(args: argparse.Namespace) -> str:
    if args.query_file:
        return open(args.query_file, "r", encoding="utf-8-sig").read().strip()
    return (args.query or "").strip()


def normalize_text(text: str) -> str:
    return text.casefold().strip()


def parse_db_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00").replace(" ", "T")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def query_terms(text: str) -> list[str]:
    terms: set[str] = set()
    normalized = normalize_text(text)
    for token in re.findall(r"[a-z0-9][a-z0-9_\-./]+", normalized):
        if len(token) >= 2:
            terms.add(token)
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        terms.add(run)
        for width in (2, 3):
            if len(run) >= width:
                for idx in range(0, len(run) - width + 1):
                    terms.add(run[idx : idx + width])
    return sorted(terms, key=len, reverse=True)


def effective_time(row: dict[str, object]) -> datetime | None:
    return parse_db_time(str(row["event_time"] or "")) or parse_db_time(str(row["created_at"] or ""))


def matches_any(value: str, filters: list[str]) -> bool:
    if not filters:
        return True
    haystack = normalize_text(value)
    return any(normalize_text(item) in haystack for item in filters if item.strip())


def text_score(row: dict[str, object], query: str, terms: list[str]) -> tuple[float, list[str]]:
    if not query:
        return 0.0, []

    content = normalize_text(str(row["content"]))
    topic_hint = normalize_text(str(row["topic_hint"]))
    domain_hint = normalize_text(str(row["domain_hint"]))
    classifier_kind = normalize_text(str(row["classifier_kind"]))
    target_memory_kind = normalize_text(str(row["target_memory_kind"]))

    normalized_query = normalize_text(query)
    score = 0.0
    reasons: list[str] = []

    if normalized_query and normalized_query in content:
        score += 3.8
        reasons.append("content matches full query")
    if normalized_query and normalized_query in topic_hint:
        score += 2.8
        reasons.append("topic matches full query")
    if normalized_query and normalized_query in domain_hint:
        score += 1.8
        reasons.append("domain matches full query")

    for term in terms:
        if len(term) < 2:
            continue
        if term in topic_hint:
            score += 1.8
            reasons.append(f"topic:{term}")
        if term in domain_hint:
            score += 1.3
            reasons.append(f"domain:{term}")
        if term in content:
            score += 1.0
            reasons.append(f"content:{term}")
        if term in classifier_kind:
            score += 0.7
            reasons.append(f"classifier:{term}")
        if term in target_memory_kind:
            score += 0.7
            reasons.append(f"target:{term}")

    return score, reasons[:6]


def snippet(text: str, limit: int = 180) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def main() -> None:
    args = parse_args()
    query = read_query(args)
    terms = query_terms(query) if query else []
    since = parse_db_time(args.since)
    until = parse_db_time(args.until)
    source_types = [item.casefold() for item in args.source_type]
    processed_states = [item.casefold() for item in args.processed_state]

    root = store_root(args.store)
    conn = open_db(root)

    clauses = []
    params: list[object] = []
    if args.subject_id:
        clauses.append("subject_id = ?")
        params.append(args.subject_id)
    if args.session_id:
        clauses.append("COALESCE(session_id, '') = ?")
        params.append(args.session_id)
    if source_types:
        placeholders = ", ".join("?" for _ in source_types)
        clauses.append(f"LOWER(source_type) IN ({placeholders})")
        params.extend(source_types)
    if processed_states:
        placeholders = ", ".join("?" for _ in processed_states)
        clauses.append(f"LOWER(processed_state) IN ({placeholders})")
        params.extend(processed_states)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT
            id,
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
            created_at,
            processed_state,
            processed_at,
            batch_id,
            classifier_kind,
            classifier_domain,
            target_memory_kind,
            target_memory_path
        FROM raw_events
        {where}
        ORDER BY id DESC
        """,
        tuple(params),
    ).fetchall()
    conn.close()

    results: list[dict[str, object]] = []
    for raw in rows:
        row = {
            "id": int(raw[0]),
            "subject_id": str(raw[1] or ""),
            "subject_name": str(raw[2] or ""),
            "session_id": str(raw[3] or ""),
            "source_type": str(raw[4] or ""),
            "source_ref": str(raw[5] or ""),
            "content": str(raw[6] or ""),
            "content_hash": str(raw[7] or ""),
            "topic_hint": str(raw[8] or ""),
            "domain_hint": str(raw[9] or ""),
            "event_time": str(raw[10] or ""),
            "created_at": str(raw[11] or ""),
            "processed_state": str(raw[12] or ""),
            "processed_at": str(raw[13] or ""),
            "batch_id": str(raw[14] or ""),
            "classifier_kind": str(raw[15] or ""),
            "classifier_domain": str(raw[16] or ""),
            "target_memory_kind": str(raw[17] or ""),
            "target_memory_path": str(raw[18] or ""),
        }
        when = effective_time(row)
        if since and (when is None or when < since):
            continue
        if until and (when is None or when > until):
            continue
        if not matches_any(row["topic_hint"], args.topic):
            continue
        if not matches_any(row["domain_hint"], args.domain):
            continue

        score, reasons = text_score(row, query, terms)
        row["effective_time"] = when.isoformat() if when else ""
        row["score"] = round(score, 4)
        row["reasons"] = reasons
        results.append(row)

    if query:
        results.sort(key=lambda item: (float(item["score"]), item["effective_time"], int(item["id"])), reverse=True)
    else:
        results.sort(key=lambda item: (item["effective_time"], int(item["id"])), reverse=True)

    final_rows = results[: max(1, args.limit)]
    payload_rows: list[dict[str, object]] = []
    for row in final_rows:
        payload = {
            "id": row["id"],
            "subject_id": row["subject_id"],
            "subject_name": row["subject_name"],
            "session_id": row["session_id"],
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
            "topic_hint": row["topic_hint"],
            "domain_hint": row["domain_hint"],
            "effective_time": row["effective_time"],
            "created_at": row["created_at"],
            "processed_state": row["processed_state"],
            "processed_at": row["processed_at"],
            "classifier_kind": row["classifier_kind"],
            "classifier_domain": row["classifier_domain"],
            "target_memory_kind": row["target_memory_kind"],
            "target_memory_path": row["target_memory_path"],
            "batch_id": row["batch_id"],
            "score": row["score"],
            "reasons": row["reasons"],
            "snippet": snippet(str(row["content"])),
        }
        if args.full_content:
            payload["content"] = row["content"]
        payload_rows.append(payload)

    emit(
        {
            "status": "ok",
            "query": query,
            "filters": {
                "subject_id": args.subject_id,
                "session_id": args.session_id,
                "topic": args.topic,
                "domain": args.domain,
                "source_type": args.source_type,
                "processed_state": args.processed_state,
                "since": args.since,
                "until": args.until,
            },
            "count": len(payload_rows),
            "results": payload_rows,
        }
    )


if __name__ == "__main__":
    main()
