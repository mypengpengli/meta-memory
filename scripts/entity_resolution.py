"""Deterministic entity aliases and typed claim links, with safe review fallback."""
from __future__ import annotations

import re
import uuid

from _common import open_db


def normalize_alias(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (value or "").casefold())


def resolve_entity(conn, *, workspace_id: str, name: str, entity_type: str, source_event_id: int | None = None, confidence: float = 0.8) -> str:
    normalized = normalize_alias(name)
    if not normalized:
        return ""
    row = conn.execute("""SELECT ea.entity_uid FROM entity_aliases ea
        JOIN entities e ON e.entity_uid=ea.entity_uid
        WHERE ea.workspace_id=? AND e.workspace_id=? AND ea.normalized_alias=?
        ORDER BY ea.confidence DESC LIMIT 1""", (workspace_id, workspace_id, normalized)).fetchone()
    if row:
        uid = str(row[0])
    else:
        exact = conn.execute("SELECT entity_uid FROM entities WHERE workspace_id=? AND canonical_name=? AND entity_type=?", (workspace_id, name, entity_type)).fetchone()
        uid = str(exact[0]) if exact else str(uuid.uuid4())
        if not exact:
            conn.execute("INSERT INTO entities(entity_uid, workspace_id, canonical_name, entity_type) VALUES(?, ?, ?, ?)", (uid, workspace_id, name, entity_type))
    conn.execute("INSERT OR IGNORE INTO entity_aliases(entity_uid, alias, normalized_alias, source_event_id, confidence, workspace_id) VALUES(?, ?, ?, ?, ?, ?)", (uid, name, normalized, source_event_id, confidence, workspace_id))
    return uid


def resolve_claim_entities(root, claim_id: str, entities: list[dict[str, object]] | None = None, *, workspace_id: str = "default") -> list[str]:
    conn = open_db(root)
    row = conn.execute("SELECT subject_text, object_text FROM claims WHERE id=?", (claim_id,)).fetchone()
    if not row:
        conn.close(); return []
    supplied = list(entities or [])
    if not supplied:
        supplied = [{"name": str(row[0] or ""), "type": "subject", "role": "subject"}, {"name": str(row[1] or ""), "type": "related", "role": "object"}]
    linked: list[str] = []
    for entity in supplied:
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        role = str(entity.get("role") or ("subject" if name == str(row[0] or "") else "related"))
        uid = resolve_entity(conn, workspace_id=workspace_id, name=name, entity_type=str(entity.get("type") or "unknown"), confidence=float(entity.get("confidence", 0.8) or 0.8))
        if uid:
            conn.execute("INSERT OR REPLACE INTO claim_entities(claim_uid, entity_uid, role, confidence) VALUES(?, ?, ?, ?)", (claim_id, uid, role, float(entity.get("confidence", 0.8) or 0.8)))
            linked.append(uid)
    conn.commit(); conn.close()
    return linked


def retrieval_seed_claim_ids(
    conn,
    query_terms: list[str],
    *,
    workspace_id: str = "default",
    limit: int = 64,
) -> set[str]:
    """Return a capped set of claims matched by entity aliases.

    Retrieval calls this before loading document projections so entity matches
    participate in recall without turning the graph into an unbounded scan.
    """

    aliases = list(dict.fromkeys(normalize_alias(term) for term in query_terms if normalize_alias(term)))
    if not aliases:
        return set()
    try:
        rows = conn.execute(
            f"""
            SELECT DISTINCT ce.claim_uid
            FROM entity_aliases AS ea
            JOIN claim_entities AS ce ON ce.entity_uid=ea.entity_uid
            WHERE ea.workspace_id=?
              AND ea.normalized_alias IN ({', '.join('?' for _ in aliases)})
            LIMIT ?
            """,
            (workspace_id, *aliases, max(1, int(limit))),
        ).fetchall()
    except Exception:
        return set()
    return {str(row[0]) for row in rows if str(row[0] or "")}


def graph_candidate_claim_ids(
    conn,
    seed_claim_ids: set[str],
    *,
    hops: int = 1,
    limit: int = 64,
) -> set[str]:
    """Return bounded graph neighbours for already relevant claims only."""

    frontier = {value for value in seed_claim_ids if value}
    discovered: set[str] = set()
    remaining = max(0, int(limit))
    for _ in range(max(0, min(int(hops), 2))):
        if not frontier or remaining <= 0:
            break
        values = sorted(frontier)
        placeholders = ", ".join("?" for _ in values)
        try:
            rows = conn.execute(
                f"""
                SELECT from_claim_id,to_claim_id
                FROM memory_edges
                WHERE from_claim_id IN ({placeholders}) OR to_claim_id IN ({placeholders})
                ORDER BY id DESC LIMIT ?
                """,
                (*values, *values, remaining),
            ).fetchall()
        except Exception:
            break
        next_frontier: set[str] = set()
        for left, right in rows:
            for value in (str(left or ""), str(right or "")):
                if value and value not in seed_claim_ids and value not in discovered:
                    discovered.add(value)
                    next_frontier.add(value)
                    if len(discovered) >= limit:
                        return discovered
        frontier = next_frontier
        remaining = max(0, limit - len(discovered))
    return discovered


def enrich_retrieval_scores(conn, items: list[dict[str, object]], query_terms: list[str], *, workspace_id: str = "default") -> None:
    matched = retrieval_seed_claim_ids(conn, query_terms, workspace_id=workspace_id, limit=max(32, len(items) * 2))
    active = {str(item.get("memory_id") or "") for item in items if float(item.get("field_score", 0.0) or 0.0) > 0}
    adjacent = graph_candidate_claim_ids(conn, active, hops=1, limit=max(32, len(items) * 2))
    for item in items:
        uid = str(item.get("memory_id") or "")
        if uid in matched:
            item["entity_score"] = float(item.get("entity_score", 0.0)) + 1.0
            item.setdefault("reasons", []).append("entity")
        if uid in adjacent:
            item["graph_score"] = float(item.get("graph_score", 0.0)) + 0.7
            item.setdefault("reasons", []).append("typed-edge")
