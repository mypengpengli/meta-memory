#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from datetime import datetime, timezone

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root
from config import get
from llm_client import embed
from runtime_identity import identity_from, visibility_sql


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
    parser.add_argument("--session-id", default="", help="Optional session id recorded with retrieval telemetry")
    parser.add_argument("--workspace-id", default="default", help="Entity/graph workspace scope")
    parser.add_argument("--profile-id", default="default", help="Profile identity scope")
    parser.add_argument("--agent-id", default="", help="Agent identity for agent-private memory")
    parser.add_argument("--valid-at", help="Retrieve facts valid at this ISO timestamp; defaults to now")
    parser.add_argument("--no-chunks", action="store_true", help="Disable chunk-level BM25 recall")
    parser.add_argument("--include-embeddings", action="store_true", help="Fuse optional external embedding results when available")
    parser.add_argument("--embedding-model", default="external")
    parser.add_argument("--rrf-k", type=int, default=60, help="Reciprocal-rank-fusion constant")
    parser.add_argument("--subject-id", help="Filter by subject_id")
    parser.add_argument("--active-subject-id", action="append", default=[], help="Additional active subject; may be repeated")
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
    if row.get("valid_to") or row["end_at"]:
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
    if row.get("valid_to") or row.get("end_at"):
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
        candidates = [row for row in rows if row["memory_kind"] == kind and row["status"] not in {"superseded", "corrected"} and int(row.get("prompt_eligible", 1) or 0) == 1]
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


def scoped_subject_clauses(filters: dict[str, object], *, alias: str) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    subjects = list(dict.fromkeys([str(filters.get("subject_id") or ""), *[str(item) for item in filters.get("active_subject_ids", []) if str(item)]]))
    subjects = [item for item in subjects if item]
    if subjects:
        clauses.append(f"{alias}.subject_id IN ({', '.join('?' for _ in subjects)})")
        params.extend(subjects)
    scope_sql, scope_params = visibility_sql(identity_from(profile_id=str(filters.get("profile_id") or "default"), workspace_id=str(filters.get("workspace_id") or "default"), agent_id=str(filters.get("agent_id") or "")), alias=alias)
    clauses.append(scope_sql)
    params.extend(scope_params)
    return clauses, params


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
    valid_at = str(filters.get("valid_at") or "")

    scope_clauses, scope_params = scoped_subject_clauses(filters, alias="d")
    clauses.extend(scope_clauses); params.extend(scope_params)
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
    clauses.append("LOWER(COALESCE(d.status, '')) NOT IN ('superseded', 'corrected')")
    clauses.append("COALESCE(d.security_state, 'clean') NOT IN ('blocked', 'suspicious')")
    clauses.append("COALESCE(d.prompt_eligible, 1)=1")
    clauses.append("(COALESCE(d.replaced_by, '') = '' OR COALESCE(d.replaced_by, '') = '[]')")
    if valid_at:
        clauses.append("(COALESCE(d.valid_to, d.end_at, '') = '' OR COALESCE(d.valid_to, d.end_at) > ?)")
        params.append(valid_at)

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


def chunk_scores(conn, terms: list[str], filters: dict[str, object], limit: int) -> dict[str, dict[str, object]]:
    query = fts_query(terms)
    if not query:
        return {}
    clauses = ["chunk_fts MATCH ?"]
    params: list[object] = [query]
    subject_id = str(filters.get("subject_id") or "")
    include_candidates = bool(filters.get("include_candidates", False))
    valid_at = str(filters.get("valid_at") or "")
    scope_clauses, scope_params = scoped_subject_clauses(filters, alias="d")
    clauses.extend(scope_clauses); params.extend(scope_params)
    if not include_candidates:
        clauses.append("LOWER(d.memory_kind) != 'candidate'")
    clauses.append("LOWER(COALESCE(d.status, '')) NOT IN ('superseded', 'corrected')")
    clauses.append("COALESCE(d.security_state, 'clean') NOT IN ('blocked', 'suspicious')")
    clauses.append("COALESCE(d.prompt_eligible, 1)=1")
    if valid_at:
        clauses.append("(COALESCE(d.valid_to, d.end_at, '') = '' OR COALESCE(d.valid_to, d.end_at) > ?)")
        params.append(valid_at)
    try:
        rows = conn.execute(
            f"""
            SELECT c.doc_path, c.heading, c.start_line, c.end_line, c.content, bm25(chunk_fts) AS rank
            FROM chunk_fts
            JOIN chunks AS c ON c.id = CAST(chunk_fts.chunk_id AS INTEGER)
            JOIN documents AS d ON d.path = c.doc_path
            WHERE {' AND '.join(clauses)}
            ORDER BY rank LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    results: dict[str, dict[str, object]] = {}
    total = max(len(rows), 1)
    for index, row in enumerate(rows):
        path = str(row[0])
        if path in results:
            continue
        results[path] = {"score": round(3.0 * (total - index) / total, 4), "heading": str(row[1] or ""), "start_line": int(row[2]), "end_line": int(row[3]), "content": str(row[4] or "")}
    return results


def embedding_scores(conn, query: str, filters: dict[str, object], model: str) -> dict[str, float]:
    vectors = embed([query])
    if not vectors or not vectors[0]:
        return {}
    query_vector = vectors[0]
    magnitude = math.sqrt(sum(value * value for value in query_vector))
    if magnitude == 0:
        return {}
    clauses = ["e.node_type='chunk'", "e.model=?"]
    params: list[object] = [model]
    scope_clauses, scope_params = scoped_subject_clauses(filters, alias="d")
    clauses.extend(scope_clauses); params.extend(scope_params)
    try:
        rows = conn.execute(
        f"SELECT c.doc_path, e.vector_json FROM embeddings e JOIN chunks c ON c.id=CAST(e.node_id AS INTEGER) JOIN documents d ON d.path=c.doc_path WHERE {' AND '.join(clauses)}",
            tuple(params),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    scores: dict[str, float] = {}
    for path, raw_vector in rows:
        try:
            vector = [float(value) for value in json.loads(raw_vector)]
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if len(vector) != len(query_vector):
            continue
        length = math.sqrt(sum(value * value for value in vector))
        if length:
            scores[str(path)] = max(scores.get(str(path), -1.0), sum(left * right for left, right in zip(query_vector, vector)) / (magnitude * length))
    return {path: score for path, score in scores.items() if score > 0}


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


def rrf_fuse(items: list[dict[str, object]], rrf_k: int) -> None:
    signal_keys = {
        "field_score": "retrieval.weights.field",
        "fts_score": "retrieval.weights.document_bm25",
        "chunk_score": "retrieval.weights.chunk_bm25",
        "embedding_score": "retrieval.weights.embedding",
        "entity_score": "retrieval.weights.entity",
        "graph_score": "retrieval.weights.graph",
        "association_score": "retrieval.weights.graph",
    }
    for row in items:
        row["rrf_score"] = 0.0
    for signal, config_key in signal_keys.items():
        weight = float(get(config_key))
        if weight <= 0:
            continue
        ranked = [row for row in items if float(row.get(signal, 0.0) or 0.0) > 0.0]
        ranked.sort(key=lambda row: float(row.get(signal, 0.0) or 0.0), reverse=True)
        for rank, row in enumerate(ranked, start=1):
            row["rrf_score"] = float(row["rrf_score"]) + weight / (max(1, rrf_k) + rank)
    for row in items:
        row["rrf_score"] = round(float(row["rrf_score"] or 0.0), 6)


def record_retrieval(conn, selected: list[dict[str, object]], query: str, filters: dict[str, object], session_id: str) -> None:
    relevant_selected = [row for row in selected if float(row.get("query_score", 0.0) or 0.0) > 0.0]
    payload = {
        "query": query,
        "filters": filters,
        "used_paths": [row["path"] for row in relevant_selected],
    }
    conn.execute(
        "INSERT INTO retrieval_log(used_paths) VALUES(?)",
        (json.dumps(payload, ensure_ascii=False),),
    )
    conn.execute(
        """
        INSERT INTO retrieval_events(subject_id, session_id, query_hash, query_text, selected_node_ids, selected_paths)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            str(filters.get("subject_id") or ""),
            session_id,
            __import__("hashlib").sha256(query.encode("utf-8")).hexdigest(),
            query,
            json.dumps([row.get("memory_id", row["path"]) for row in relevant_selected], ensure_ascii=False),
            json.dumps([row["path"] for row in relevant_selected], ensure_ascii=False),
        ),
    )
    conn.commit()


def retrieve(args: argparse.Namespace, *, query: str | None = None) -> dict[str, object]:
    query = query if query is not None else read_query(args)
    root = store_root(args.store)
    conn = open_db(root)
    domains = [item.casefold() for item in args.domain]
    kinds = [item.casefold() for item in args.memory_kind]
    filters = {
        "subject_id": args.subject_id or "",
        "active_subject_ids": args.active_subject_id,
        "profile_id": args.profile_id,
        "workspace_id": args.workspace_id,
        "agent_id": args.agent_id,
        "subject_name": args.subject_name or "",
        "domains": args.domain,
        "memory_kinds": args.memory_kind,
        "include_candidates": args.include_candidates,
        "valid_at": args.valid_at or datetime.now(timezone.utc).isoformat(),
    }
    terms = query_terms(query)
    fts_score_map = fts_scores(conn, terms, filters, max(args.candidate_pool, args.top_k * 4))
    chunk_score_map = {} if args.no_chunks else chunk_scores(conn, terms, filters, max(args.candidate_pool, args.top_k * 4))
    embedding_score_map = embedding_scores(conn, query, filters, args.embedding_model) if args.include_embeddings else {}

    clauses = []
    params: list[object] = []
    scope_clauses, scope_params = scoped_subject_clauses(filters, alias="d")
    clauses.extend(scope_clauses); params.extend(scope_params)
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
    clauses.append("LOWER(COALESCE(d.status, '')) NOT IN ('superseded', 'corrected')")
    clauses.append("COALESCE(d.security_state, 'clean') NOT IN ('blocked', 'suspicious')")
    clauses.append("COALESCE(d.prompt_eligible, 1)=1")
    clauses.append("(COALESCE(d.replaced_by, '') = '' OR COALESCE(d.replaced_by, '') = '[]')")
    clauses.append("(COALESCE(d.valid_to, d.end_at, '') = '' OR COALESCE(d.valid_to, d.end_at) > ?)")
    params.append(str(filters["valid_at"]))

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
            d.valid_from,
            d.valid_to,
            d.verification_state,
            d.security_state,
            d.prompt_eligible,
            d.memory_id,
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
        "valid_from",
        "valid_to",
        "verification_state",
        "security_state",
        "prompt_eligible",
        "memory_id",
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
        chunk = chunk_score_map.get(str(row["path"]), {})
        chunk_score = float(chunk.get("score", 0.0) or 0.0)
        life_score, life_reasons = lifecycle_score(row)
        if fts_score:
            reasons.append(fts_reason)
        if chunk_score:
            reasons.append("chunk/bm25")
        row["field_score"] = round(rel_score, 4)
        row["query_score"] = round(rel_score, 4)
        row["fts_score"] = round(fts_score, 4)
        row["chunk_score"] = round(chunk_score, 4)
        row["embedding_score"] = round(float(embedding_score_map.get(str(row["path"]), 0.0) or 0.0), 4)
        row["entity_score"] = 0.0
        row["graph_score"] = 0.0
        row["best_chunk"] = {key: chunk[key] for key in ("heading", "start_line", "end_line", "content") if key in chunk}
        row["association_score"] = 0.0
        row["lifecycle_score"] = round(life_score, 4)
        row["total_score"] = round(base_score(row) + life_score, 4)
        row["reasons"] = (reasons + life_reasons)[:6]
        items.append(row)

    from entity_resolution import enrich_retrieval_scores
    enrich_retrieval_scores(conn, items, terms, workspace_id=args.workspace_id)

    expand_associations(items, args.expand_hops)
    rrf_fuse(items, args.rrf_k)
    for row in items:
        row["total_score"] = round(float(row["total_score"]) + float(row["rrf_score"]) * 100.0, 4)
        row["query_score"] = round(
            float(row["query_score"])
            + float(row.get("fts_score", 0.0) or 0.0)
            + float(row.get("chunk_score", 0.0) or 0.0)
            + float(row.get("embedding_score", 0.0) or 0.0)
            + float(row.get("entity_score", 0.0) or 0.0)
            + float(row.get("graph_score", 0.0) or 0.0)
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

    record_retrieval(
        conn,
        selected,
        query,
        filters,
        args.session_id,
    )
    conn.close()

    return {
            "status": "ok",
            "query": query,
            "terms": terms[:20],
            "returned": len(selected),
            "selected": [
                {
                    # Public callers need a stable identifier to submit
                    # source-backed corrections through the public CLI.
                    "id": row["memory_id"],
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
                    "chunk_score": row["chunk_score"],
                    "embedding_score": row["embedding_score"],
                    "rrf_score": row["rrf_score"],
                    "association_score": row["association_score"],
                    "lifecycle_score": row["lifecycle_score"],
                    "best_chunk": row["best_chunk"],
                    "reasons": row["reasons"],
                }
                for row in selected
            ],
    }


def main() -> None:
    emit(retrieve(parse_args()))


if __name__ == "__main__":
    main()
