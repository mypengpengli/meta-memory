#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
from datetime import datetime, timezone

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root


KIND_BIAS = {
    "profile": 2.4,
    "state": 2.0,
    "goal": 1.6,
    "relationship": 1.5,
    "event": 1.3,
    "domain": 1.1,
    "session": 0.9,
    "candidate": 0.2,
    "note": 0.5,
}

STATUS_BIAS = {
    "active": 0.7,
    "historical": -0.1,
    "pending": -0.4,
    "superseded": -1.2,
}

BASIC_KINDS = ["profile", "state"]

PAGE_ROLE_BIAS = {
    "person-profile": 1.6,
    "state-current": 1.4,
    "goals-projects": 1.1,
    "relationships-current": 1.1,
    "timeline-index": 0.9,
    "domains-index": 0.8,
    "domain-current": 0.7,
    "session-current": 0.5,
    "candidate-pool": 0.2,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Retrieve the most relevant memories for a question.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--query", help="The current question or task")
    parser.add_argument("--query-file", help="Read the query from a UTF-8 text file")
    parser.add_argument("--top-k", type=int, default=6, help="Maximum memories to return")
    parser.add_argument("--subject-id", help="Filter by subject_id")
    parser.add_argument("--subject-name", help="Filter by subject_name")
    parser.add_argument("--domain", action="append", default=[], help="Filter by domain; may be repeated")
    parser.add_argument(
        "--memory-kind",
        action="append",
        default=[],
        help="Filter by memory_kind; may be repeated",
    )
    parser.add_argument(
        "--include-candidates",
        action="store_true",
        help="Include candidate memories in the ranked results",
    )
    parser.add_argument(
        "--no-basics",
        action="store_true",
        help="Do not prioritize relevant profile/state memories at the front",
    )
    return parser.parse_args()


def read_query(args: argparse.Namespace) -> str:
    if args.query_file:
        return open(args.query_file, "r", encoding="utf-8-sig").read().strip()
    if args.query:
        return args.query.strip()
    raise SystemExit("Either --query or --query-file is required.")


def parse_json_list(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return [raw]
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item).strip()]
    return [str(parsed)]


def normalize_text(text: str) -> str:
    return text.casefold().strip()


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


def text_fields(row: dict[str, object]) -> dict[str, str]:
    return {
        "title": normalize_text(str(row["title"])),
        "summary": normalize_text(str(row["summary"])),
        "topic": normalize_text(str(row["topic"])),
        "domain": normalize_text(str(row["domain"])),
        "tags": normalize_text(" ".join(parse_json_list(str(row["tags"])))),
        "source": normalize_text(str(row["source"])),
        "related_people": normalize_text(" ".join(parse_json_list(str(row["related_people"])))),
        "related_events": normalize_text(" ".join(parse_json_list(str(row["related_events"])))),
        "related_topics": normalize_text(" ".join(parse_json_list(str(row["related_topics"])))),
        "subject_name": normalize_text(str(row["subject_name"])),
    }


def relevance(row: dict[str, object], query: str, terms: list[str]) -> tuple[float, list[str]]:
    fields = text_fields(row)
    normalized_query = normalize_text(query)
    score = 0.0
    reasons: list[str] = []

    if normalized_query and normalized_query in fields["title"]:
        score += 4.0
        reasons.append("title matches full query")
    elif normalized_query and normalized_query in fields["summary"]:
        score += 3.0
        reasons.append("summary matches full query")

    for term in terms:
        if len(term) < 2:
            continue
        term_score = 0.0
        term_reasons: list[str] = []
        if term in fields["title"]:
            term_score += 2.1
            term_reasons.append(f"title:{term}")
        if term in fields["topic"]:
            term_score += 1.6
            term_reasons.append(f"topic:{term}")
        if term in fields["summary"]:
            term_score += 1.2
            term_reasons.append(f"summary:{term}")
        if term in fields["tags"]:
            term_score += 1.1
            term_reasons.append(f"tags:{term}")
        if term in fields["domain"]:
            term_score += 0.9
            term_reasons.append(f"domain:{term}")
        if term in fields["related_people"]:
            term_score += 1.0
            term_reasons.append(f"related_people:{term}")
        if term in fields["related_events"]:
            term_score += 0.9
            term_reasons.append(f"related_events:{term}")
        if term in fields["related_topics"]:
            term_score += 1.0
            term_reasons.append(f"related_topics:{term}")
        if term in fields["subject_name"]:
            term_score += 1.0
            term_reasons.append(f"subject:{term}")
        if term_score:
            score += term_score
            reasons.extend(term_reasons[:2])

    return score, reasons[:5]


def base_score(row: dict[str, object]) -> float:
    rank_score = float(row["rank_score"] or 0.0)
    kind = str(row["memory_kind"])
    status = str(row["status"])
    score = rank_score + KIND_BIAS.get(kind, 0.4) + STATUS_BIAS.get(status, 0.0)
    page_role = str(row.get("page_role", "") or "")
    score += PAGE_ROLE_BIAS.get(page_role, 0.0)
    if int(row.get("canonical", 0) or 0) == 1:
        score += 0.6
    if row["end_at"]:
        score -= 0.2
    return score


def select_basics(rows: list[dict[str, object]], top_k: int) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    for kind in BASIC_KINDS:
        if len(selected) >= top_k:
            break
        candidates = [row for row in rows if row["memory_kind"] == kind and row["status"] != "superseded"]
        canonical = [row for row in candidates if int(row.get("canonical", 0) or 0) == 1]
        preferred = canonical[0] if canonical else (candidates[0] if candidates else None)
        if preferred:
            selected.append(preferred)
    return selected


def update_retrieval_stats(conn, selected: list[dict[str, object]], query: str, filters: dict[str, object]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    relevant_selected = [row for row in selected if float(row.get("query_score", 0.0) or 0.0) > 0.0]
    for row in relevant_selected:
        hit_count = int(row["hit_count"] or 0) + 1
        confidence = float(row["score_confidence"] or row["confidence"] or 0.0)
        rank_score = round(math.log1p(hit_count) + confidence, 4)
        conn.execute(
            """
            INSERT INTO scores(path, hit_count, confidence, rank_score, last_hit_at)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                hit_count=excluded.hit_count,
                confidence=excluded.confidence,
                rank_score=excluded.rank_score,
                last_hit_at=excluded.last_hit_at
            """,
            (row["path"], hit_count, confidence, rank_score, now),
        )
    payload = {
        "query": query,
        "filters": filters,
        "used_paths": [row["path"] for row in relevant_selected],
    }
    conn.execute(
        "INSERT INTO retrieval_log(used_paths) VALUES(?)",
        (json.dumps(payload, ensure_ascii=False),),
    )
    conn.commit()


def main() -> None:
    args = parse_args()
    query = read_query(args)
    root = store_root(args.store)
    conn = open_db(root)
    domains = [item.casefold() for item in args.domain]
    kinds = [item.casefold() for item in args.memory_kind]

    clauses = []
    params: list[object] = []
    if args.subject_id:
        clauses.append("d.subject_id = ?")
        params.append(args.subject_id)
    if args.subject_name:
        clauses.append("LOWER(d.subject_name) = ?")
        params.append(args.subject_name.casefold())
    if domains:
        placeholders = ", ".join("?" for _ in domains)
        clauses.append(f"LOWER(d.domain) IN ({placeholders})")
        params.extend(domains)
    if kinds:
        placeholders = ", ".join("?" for _ in kinds)
        clauses.append(f"LOWER(d.memory_kind) IN ({placeholders})")
        params.extend(kinds)
    if not args.include_candidates:
        clauses.append("LOWER(d.memory_kind) != 'candidate'")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT
            d.path,
            d.title,
            d.subject_id,
            d.subject_name,
            d.memory_kind,
            d.page_role,
            d.canonical,
            d.domain,
            d.topic,
            d.tags,
            d.summary,
            d.confidence,
            d.status,
            d.source,
            d.start_at,
            d.end_at,
            d.related_people,
            d.related_events,
            d.related_topics,
            COALESCE(s.hit_count, 0) AS hit_count,
            COALESCE(s.confidence, d.confidence, 0.0) AS score_confidence,
            COALESCE(s.rank_score, 0.0) AS rank_score,
            COALESCE(s.last_hit_at, '') AS last_hit_at
        FROM documents AS d
        LEFT JOIN scores AS s ON s.path = d.path
        {where}
        """,
        tuple(params),
    ).fetchall()

    columns = [
        "path",
        "title",
        "subject_id",
        "subject_name",
        "memory_kind",
        "page_role",
        "canonical",
        "domain",
        "topic",
        "tags",
        "summary",
        "confidence",
        "status",
        "source",
        "start_at",
        "end_at",
        "related_people",
        "related_events",
        "related_topics",
        "hit_count",
        "score_confidence",
        "rank_score",
        "last_hit_at",
    ]
    items: list[dict[str, object]] = []
    terms = query_terms(query)
    for raw in rows:
        row = dict(zip(columns, raw))
        rel_score, reasons = relevance(row, query, terms)
        total_score = round(base_score(row) + rel_score, 4)
        row["query_score"] = round(rel_score, 4)
        row["total_score"] = total_score
        row["reasons"] = reasons
        items.append(row)

    items.sort(key=lambda item: (float(item["total_score"]), float(item["query_score"])), reverse=True)
    relevant_items = [item for item in items if float(item["query_score"]) > 0.0]

    selected: list[dict[str, object]] = []
    selected_paths: set[str] = set()
    if not args.no_basics:
        for row in select_basics(relevant_items, args.top_k):
            if row["path"] not in selected_paths:
                selected.append(row)
                selected_paths.add(str(row["path"]))

    for row in relevant_items:
        if len(selected) >= args.top_k:
            break
        if row["path"] in selected_paths:
            continue
        selected.append(row)
        selected_paths.add(str(row["path"]))

    update_retrieval_stats(
        conn,
        selected,
        query,
        {
            "subject_id": args.subject_id or "",
            "subject_name": args.subject_name or "",
            "domains": args.domain,
            "memory_kinds": args.memory_kind,
            "include_candidates": args.include_candidates,
        },
    )
    conn.close()

    emit(
        {
            "status": "ok",
            "query": query,
            "terms": terms[:20],
            "returned": len(selected),
            "selected": [
                {
                    "path": row["path"],
                    "title": row["title"],
                    "memory_kind": row["memory_kind"],
                    "page_role": row["page_role"],
                    "canonical": bool(row["canonical"]),
                    "domain": row["domain"],
                    "topic": row["topic"],
                    "summary": row["summary"],
                    "score": row["total_score"],
                    "query_score": row["query_score"],
                    "reasons": row["reasons"],
                }
                for row in selected
            ],
        }
    )


if __name__ == "__main__":
    main()
