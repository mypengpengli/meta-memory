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
MAX_EXPAND_HOPS = 2
STOP_TERMS = {
    "a",
    "an",
    "and",
    "are",
    "for",
    "how",
    "is",
    "me",
    "my",
    "of",
    "the",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
    "我",
    "你",
    "什么",
    "怎么",
    "如何",
    "今天",
    "现在",
}

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
    parser.add_argument("--candidate-pool", type=int, default=24, help="Maximum internally ranked candidates before final trimming")
    parser.add_argument("--expand-hops", type=int, default=1, help="Association expansion hops through related fields, 0-2")
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
    return sorted((term for term in terms if term not in STOP_TERMS), key=len, reverse=True)


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
        "related_sources": normalize_text(" ".join(parse_json_list(str(row["related_sources"])))),
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
        if term in fields["related_sources"]:
            term_score += 0.7
            term_reasons.append(f"related_sources:{term}")
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
    score += max(0.0, min(float(row.get("importance", 0.5) or 0.5), 1.0))
    page_role = str(row.get("page_role", "") or "")
    score += PAGE_ROLE_BIAS.get(page_role, 0.0)
    if int(row.get("canonical", 0) or 0) == 1:
        score += 0.6
    if row["end_at"]:
        score -= 0.2
    return score


def lifecycle_score(row: dict[str, object]) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    status = str(row.get("status", "") or "").casefold()
    replaced_by = parse_json_list(str(row.get("replaced_by", "") or ""))
    if status == "superseded" or replaced_by:
        score -= 5.0
        reasons.append("lifecycle:superseded")
    if row.get("end_at"):
        score -= 0.8
        reasons.append("lifecycle:ended")
    if status == "active":
        score += 0.2
    if row.get("last_hit_at"):
        score += 0.2
    return score, reasons[:2]


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


def quote_fts_term(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def fts_query(terms: list[str]) -> str:
    selected = [term for term in terms if len(term) >= 2][:18]
    return " OR ".join(quote_fts_term(term) for term in selected)


def fts_scores(conn, terms: list[str], filters: dict[str, object], limit: int) -> dict[str, tuple[float, str]]:
    query = fts_query(terms)
    if not query:
        return {}

    clauses = ["document_fts MATCH ?"]
    params: list[object] = [query]
    subject_id = str(filters.get("subject_id") or "")
    subject_name = str(filters.get("subject_name") or "")
    domains = [str(item).casefold() for item in filters.get("domains", [])]
    kinds = [str(item).casefold() for item in filters.get("memory_kinds", [])]
    include_candidates = bool(filters.get("include_candidates", False))

    if subject_id:
        clauses.append("d.subject_id = ?")
        params.append(subject_id)
    if subject_name:
        clauses.append("LOWER(d.subject_name) = ?")
        params.append(subject_name.casefold())
    if domains:
        placeholders = ", ".join("?" for _ in domains)
        clauses.append(f"LOWER(d.domain) IN ({placeholders})")
        params.extend(domains)
    if kinds:
        placeholders = ", ".join("?" for _ in kinds)
        clauses.append(f"LOWER(d.memory_kind) IN ({placeholders})")
        params.extend(kinds)
    if not include_candidates:
        clauses.append("LOWER(d.memory_kind) != 'candidate'")
    clauses.append("LOWER(COALESCE(d.status, '')) != 'superseded'")
    clauses.append("(COALESCE(d.replaced_by, '') = '' OR COALESCE(d.replaced_by, '') = '[]')")

    try:
        rows = conn.execute(
            f"""
            SELECT d.path, bm25(document_fts) AS rank
            FROM document_fts
            JOIN documents AS d ON d.path = document_fts.path
            WHERE {' AND '.join(clauses)}
            ORDER BY rank
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    except Exception:
        return {}

    scores: dict[str, tuple[float, str]] = {}
    total = max(len(rows), 1)
    for idx, (path, _rank) in enumerate(rows):
        score = round(3.0 * (total - idx) / total, 4)
        scores[str(path)] = (score, "fts/bm25")
    return scores


def link_values(row: dict[str, object]) -> dict[str, set[str]]:
    values = {
        "related_people": set(parse_json_list(str(row.get("related_people", "") or ""))),
        "related_events": set(parse_json_list(str(row.get("related_events", "") or ""))),
        "related_topics": set(parse_json_list(str(row.get("related_topics", "") or ""))),
        "related_sources": set(parse_json_list(str(row.get("related_sources", "") or ""))),
    }
    topic = str(row.get("topic", "") or "").strip()
    if topic:
        values["related_topics"].add(topic)
    source = str(row.get("source", "") or "").strip()
    if source:
        values["related_sources"].add(source)
    return {key: {normalize_text(item) for item in items if normalize_text(item)} for key, items in values.items()}


def expand_associations(items: list[dict[str, object]], expand_hops: int) -> None:
    hops = max(0, min(expand_hops, MAX_EXPAND_HOPS))
    if hops <= 0:
        return

    activated = {
        str(item["path"])
        for item in items
        if float(item.get("query_score", 0.0) or 0.0) > 0.0 or float(item.get("fts_score", 0.0) or 0.0) > 0.0
    }
    frontier = [item for item in items if str(item["path"]) in activated]

    for hop in range(1, hops + 1):
        if not frontier:
            return
        seeds: dict[str, set[str]] = {
            "related_people": set(),
            "related_events": set(),
            "related_topics": set(),
            "related_sources": set(),
        }
        for item in frontier:
            for key, values in link_values(item).items():
                seeds[key].update(values)

        next_frontier: list[dict[str, object]] = []
        for item in items:
            path = str(item["path"])
            if path in activated:
                continue
            overlaps: list[str] = []
            values = link_values(item)
            for key, weight in [
                ("related_topics", 1.2),
                ("related_people", 1.1),
                ("related_events", 1.0),
                ("related_sources", 0.8),
            ]:
                shared = sorted(values[key] & seeds[key])
                if shared:
                    item["association_score"] = float(item.get("association_score", 0.0) or 0.0) + (weight / hop)
                    overlaps.append(f"{key}:{shared[0]}")
            if overlaps:
                item.setdefault("reasons", [])
                item["reasons"] = list(item["reasons"]) + [f"hop{hop}:{overlaps[0]}"]
                activated.add(path)
                next_frontier.append(item)
        frontier = next_frontier


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
    filters = {
        "subject_id": args.subject_id or "",
        "subject_name": args.subject_name or "",
        "domains": args.domain,
        "memory_kinds": args.memory_kind,
        "include_candidates": args.include_candidates,
    }
    terms = query_terms(query)
    fts_score_map = fts_scores(conn, terms, filters, max(args.candidate_pool, args.top_k * 4))

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
    clauses.append("LOWER(COALESCE(d.status, '')) != 'superseded'")
    clauses.append("(COALESCE(d.replaced_by, '') = '' OR COALESCE(d.replaced_by, '') = '[]')")

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
            d.importance,
            d.status,
            d.source,
            d.start_at,
            d.end_at,
            d.related_people,
            d.related_events,
            d.related_topics,
            d.related_sources,
            d.supersedes,
            d.replaced_by,
            d.mtime,
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
        "importance",
        "status",
        "source",
        "start_at",
        "end_at",
        "related_people",
        "related_events",
        "related_topics",
        "related_sources",
        "supersedes",
        "replaced_by",
        "mtime",
        "hit_count",
        "score_confidence",
        "rank_score",
        "last_hit_at",
    ]
    items: list[dict[str, object]] = []
    for raw in rows:
        row = dict(zip(columns, raw))
        rel_score, reasons = relevance(row, query, terms)
        fts_score, fts_reason = fts_score_map.get(str(row["path"]), (0.0, ""))
        life_score, life_reasons = lifecycle_score(row)
        if fts_score:
            reasons.append(fts_reason)
        row["query_score"] = round(rel_score, 4)
        row["fts_score"] = round(fts_score, 4)
        row["association_score"] = 0.0
        row["lifecycle_score"] = round(life_score, 4)
        row["total_score"] = round(base_score(row) + rel_score + fts_score + life_score, 4)
        row["reasons"] = (reasons + life_reasons)[:6]
        items.append(row)

    expand_associations(items, args.expand_hops)
    for row in items:
        row["total_score"] = round(float(row["total_score"]) + float(row.get("association_score", 0.0) or 0.0), 4)
        row["query_score"] = round(
            float(row["query_score"])
            + float(row.get("fts_score", 0.0) or 0.0)
            + float(row.get("association_score", 0.0) or 0.0),
            4,
        )
        row["reasons"] = list(row.get("reasons", []))[:6]

    items.sort(key=lambda item: (float(item["total_score"]), float(item["query_score"])), reverse=True)
    relevant_items = [
        item
        for item in items
        if float(item["query_score"]) > 0.0
        and not parse_json_list(str(item.get("replaced_by", "") or ""))
        and str(item.get("status", "") or "").casefold() != "superseded"
    ][: max(args.candidate_pool, args.top_k)]

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
        filters,
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
                    "importance": row["importance"],
                    "score": row["total_score"],
                    "query_score": row["query_score"],
                    "fts_score": row["fts_score"],
                    "association_score": row["association_score"],
                    "lifecycle_score": row["lifecycle_score"],
                    "reasons": row["reasons"],
                }
                for row in selected
            ],
        }
    )


if __name__ == "__main__":
    main()
