#!/usr/bin/env python3
"""Scoped node search across claims, candidate units, and indexed documents."""
from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root
from runtime_identity import identity_from, visibility_sql


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search memory nodes without returning full source content.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--subject-id", required=True, help="Required scope boundary")
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--include-units", action="store_true")
    parser.add_argument("--valid-at", help="ISO timestamp; defaults to now")
    parser.add_argument("--profile-id", default="default")
    parser.add_argument("--workspace-id", default="default")
    parser.add_argument("--agent-id", default="")
    return parser.parse_args()


def terms(text: str) -> list[str]:
    values = {token for token in re.findall(r"[\w.-]{2,}", text.casefold())}
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        values.add(run)
        values.update(run[idx : idx + 2] for idx in range(len(run) - 1))
    return sorted(values, key=len, reverse=True)


def score(query: str, value: str, topic: str = "", title: str = "") -> tuple[float, list[str]]:
    q = query.casefold().strip()
    text = value.casefold()
    topic = topic.casefold()
    title = title.casefold()
    total = 0.0
    reasons: list[str] = []
    if q and q in text:
        total += 4.0
        reasons.append("content")
    for term in terms(query):
        if term in title:
            total += 2.2
            reasons.append(f"title:{term}")
        if term in topic:
            total += 2.0
            reasons.append(f"topic:{term}")
        if term in text:
            total += 1.0
            reasons.append(f"content:{term}")
    return total, list(dict.fromkeys(reasons))[:5]


def snippet(value: str, limit: int = 220) -> str:
    flat = " ".join(value.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def search_nodes(root, subject_id: str, query: str, *, limit: int = 12, include_units: bool = False, valid_at: str | None = None, profile_id: str = "default", workspace_id: str = "default", agent_id: str = "") -> dict[str, object]:
    conn = open_db(root)
    instant = valid_at or datetime.now(timezone.utc).isoformat()
    historical = valid_at is not None
    nodes: list[dict[str, object]] = []
    identity = identity_from(profile_id=profile_id, workspace_id=workspace_id, agent_id=agent_id)
    claim_scope, claim_scope_params = visibility_sql(identity, alias="c")
    claims = conn.execute(
        """
        SELECT id, memory_kind, topic, title, content, confidence, importance, status, verification_state,
               valid_from, valid_to, support_count, memory_path
        FROM claims c
        WHERE c.subject_id=? AND """ + claim_scope + """ AND c.status != 'corrected'
          AND (?=1 OR status != 'superseded')
          AND (valid_from IS NULL OR valid_from='' OR valid_from<=?)
          AND (valid_to IS NULL OR valid_to='' OR valid_to>?)
        """,
        (subject_id, *claim_scope_params, 1 if historical else 0, instant, instant),
    ).fetchall()
    for row in claims:
        value = f"{row[2]} {row[3]} {row[4]}"
        raw_score, reasons = score(query, value, str(row[2] or ""), str(row[3] or ""))
        if raw_score:
            nodes.append({"node_id": str(row[0]), "node_type": "claim", "memory_kind": str(row[1]), "topic": str(row[2]), "title": str(row[3]), "summary": snippet(str(row[4])), "confidence": float(row[5] or 0), "importance": float(row[6] or 0), "status": str(row[7]), "verification_state": str(row[8]), "valid_from": str(row[9] or ""), "valid_to": str(row[10] or ""), "support_count": int(row[11] or 0), "path": str(row[12] or ""), "score": raw_score + float(row[5] or 0) + float(row[6] or 0), "reasons": reasons})
    if include_units:
        unit_scope, unit_scope_params = visibility_sql(identity, alias="u")
        units = conn.execute("SELECT id, unit_kind, topic, content, confidence, importance, sensitivity, status FROM memory_units u WHERE u.subject_id=? AND " + unit_scope + " AND u.status IN ('pending', 'candidate')", (subject_id, *unit_scope_params)).fetchall()
        for row in units:
            raw_score, reasons = score(query, f"{row[2]} {row[3]}", str(row[2] or ""), str(row[2] or ""))
            if raw_score:
                nodes.append({"node_id": f"unit:{row[0]}", "node_type": "unit", "memory_kind": str(row[1]), "topic": str(row[2]), "title": str(row[2]), "summary": snippet(str(row[3])), "confidence": float(row[4] or 0), "importance": float(row[5] or 0), "sensitivity": str(row[6]), "status": str(row[7]), "score": raw_score + float(row[4] or 0), "reasons": reasons})
    doc_scope, doc_scope_params = visibility_sql(identity, alias="d")
    docs = conn.execute("SELECT path, memory_kind, topic, title, summary, confidence, importance, status FROM documents d WHERE d.subject_id=? AND " + doc_scope + " AND d.status NOT IN ('superseded', 'corrected')", (subject_id, *doc_scope_params)).fetchall()
    for row in docs:
        raw_score, reasons = score(query, f"{row[2]} {row[3]} {row[4]}", str(row[2] or ""), str(row[3] or ""))
        if raw_score:
            nodes.append({"node_id": f"doc:{row[0]}", "node_type": "document", "memory_kind": str(row[1]), "topic": str(row[2]), "title": str(row[3]), "summary": snippet(str(row[4])), "confidence": float(row[5] or 0), "importance": float(row[6] or 0), "status": str(row[7]), "path": str(row[0]), "score": raw_score + float(row[5] or 0), "reasons": reasons})
    conn.close()
    nodes.sort(key=lambda item: (float(item["score"]), float(item.get("importance", 0))), reverse=True)
    return {"status": "ok", "subject_id": subject_id, "query": query, "valid_at": instant, "returned": len(nodes[: max(1, limit)]), "nodes": nodes[: max(1, limit)]}


def main() -> None:
    args = parse_args()
    emit(search_nodes(store_root(args.store), args.subject_id, args.query, limit=args.limit, include_units=args.include_units, valid_at=args.valid_at, profile_id=args.profile_id, workspace_id=args.workspace_id, agent_id=args.agent_id))


if __name__ == "__main__":
    main()
