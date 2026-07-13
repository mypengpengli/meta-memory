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


def enrich_retrieval_scores(conn, items: list[dict[str, object]], query_terms: list[str], *, workspace_id: str = "default") -> None:
    aliases = [normalize_alias(term) for term in query_terms if normalize_alias(term)]
    if not aliases:
        return
    placeholders = ", ".join("?" for _ in aliases)
    rows = conn.execute(f"SELECT DISTINCT ce.claim_uid FROM entity_aliases ea JOIN claim_entities ce ON ce.entity_uid=ea.entity_uid WHERE ea.workspace_id=? AND ea.normalized_alias IN ({placeholders})", (workspace_id, *aliases)).fetchall()
    matched = {str(row[0]) for row in rows}
    active = {str(item.get("memory_id") or "") for item in items if float(item.get("field_score", 0.0) or 0.0) > 0}
    edges = conn.execute("SELECT from_claim_id, to_claim_id FROM memory_edges").fetchall()
    adjacent = {str(right) for left, right in edges if str(left) in active} | {str(left) for left, right in edges if str(right) in active}
    for item in items:
        uid = str(item.get("memory_id") or "")
        if uid in matched:
            item["entity_score"] = float(item.get("entity_score", 0.0)) + 1.0
            item.setdefault("reasons", []).append("entity")
        if uid in adjacent:
            item["graph_score"] = float(item.get("graph_score", 0.0)) + 0.7
            item.setdefault("reasons", []).append("typed-edge")
