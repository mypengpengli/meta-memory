#!/usr/bin/env python3
"""Turn atomic units into explicit semantic consolidation plans."""
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root
from node_search import search_nodes
from llm_client import complete


RELATION_TO_ACTION = {
    "EXACT_SAME": "CORROBORATE", "SEMANTIC_SAME": "CORROBORATE",
    "NARROWS": "REFINE", "BROADENS": "REFINE", "ADDS_QUALIFIER": "REFINE",
    "CORRECTS_FALSE_FACT": "CORRECT", "REPLACES_OLD_STATE": "SUPERSEDE",
    "CONTRADICTS_UNRESOLVED": "CREATE", "UNRELATED": "CREATE", "NOT_MEMORY": "IGNORE",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a semantic, reviewable consolidation plan; never writes directly.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--policy", choices=["conservative", "balanced", "aggressive"], default="conservative")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--out-file")
    return parser.parse_args()


def choose_kind(unit_kind: str, confidence: float, sensitivity: str, policy: str) -> tuple[str, str]:
    if policy == "conservative" or sensitivity == "sensitive":
        return "candidate", "unverified"
    if policy == "balanced" and confidence >= 0.85:
        return unit_kind, "verified"
    if policy == "aggressive" and confidence >= 0.7:
        return unit_kind, "verified"
    return "candidate", "unverified"


def _tokens(value: str) -> set[str]:
    return {token for token in value.casefold().replace("-", " ").split() if len(token) > 2}


def semantic_relation(unit: dict[str, object], candidates: list[dict[str, object]]) -> tuple[str, dict[str, object] | None]:
    """Use an optional relation-only LLM, then fall back deterministically.

    The model can select a relation and an existing candidate id, never an
    action, source id, or arbitrary target. Deterministic policy maps that
    relation to the actual plan action.
    """
    allowed = set(RELATION_TO_ACTION)
    try:
        response = complete(
            "Classify one memory unit against candidate claims. Return JSON {relation, target_claim_id}. "
            "relation must be one of EXACT_SAME, SEMANTIC_SAME, NARROWS, BROADENS, ADDS_QUALIFIER, "
            "CORRECTS_FALSE_FACT, REPLACES_OLD_STATE, CONTRADICTS_UNRESOLVED, UNRELATED, NOT_MEMORY. "
            "Do not invent ids or actions.",
            {"unit": unit, "candidates": candidates[:8]},
        )
        relation = str((response or {}).get("relation") or "")
        target_id = str((response or {}).get("target_claim_id") or "")
        target = next((item for item in candidates if item["id"] == target_id), None)
        if relation in allowed and (relation in {"UNRELATED", "NOT_MEMORY"} or target is not None):
            return relation, target
    except Exception:
        pass
    for candidate in candidates:
        if str(candidate.get("content_hash", "")) == str(unit["content_hash"]):
            return "EXACT_SAME", candidate
        same_predicate = str(candidate.get("predicate") or "") == str(unit.get("predicate") or "")
        same_subject = str(candidate.get("subject_text") or "").casefold() == str(unit.get("subject_text") or "").casefold()
        same_object = str(candidate.get("object_text") or "").casefold() == str(unit.get("object_text") or "").casefold()
        if same_predicate and same_subject and same_object:
            return "SEMANTIC_SAME", candidate
        if same_predicate and same_subject and not same_object:
            if str(unit.get("unit_kind")) == "state" or str(candidate.get("memory_kind")) == "state":
                return "REPLACES_OLD_STATE", candidate
            return "CONTRADICTS_UNRESOLVED", candidate
        old_tokens, new_tokens = _tokens(str(candidate.get("content") or "")), _tokens(str(unit.get("content") or ""))
        if old_tokens and new_tokens and len(old_tokens & new_tokens) / max(1, min(len(old_tokens), len(new_tokens))) >= 0.7:
            return "ADDS_QUALIFIER", candidate
    return "UNRELATED", None


def _sources(conn, claim_id: str) -> list[int]:
    return [int(row[0]) for row in conn.execute("SELECT raw_event_id FROM claim_sources WHERE claim_id=? ORDER BY raw_event_id", (claim_id,))]


def build_plan_for_unit(root: Path, unit: dict[str, object], *, policy: str) -> dict[str, object]:
    conn = open_db(root)
    exact = conn.execute("SELECT id, title, memory_kind FROM claims WHERE subject_id=? AND content_hash=? AND status NOT IN ('superseded', 'corrected')", (unit["subject_id"], unit["content_hash"])).fetchone()
    base = {"plan_id": str(uuid.uuid4()), "subject_id": unit["subject_id"], "unit_id": unit["id"], "source_event_ids": list(unit["source_event_ids"]), "topic": unit["topic"], "domain": unit["domain"], "confidence": round(float(unit["confidence"]), 3), "uncertainty": round(float(unit["uncertainty"]), 3), "importance": round(float(unit["importance"]), 3), "durability": round(float(unit["durability"]), 3), "sensitivity": unit["sensitivity"], "predicate": unit["predicate"], "subject_text": unit["subject_text"], "object_text": unit["object_text"], "qualifiers": unit["qualifiers"], "valid_from": unit["valid_from"], "valid_to": unit["valid_to"], "observed_at": unit["observed_at"], "entities": unit["entities"]}
    if exact:
        conn.close()
        return {**base, "action": "CORROBORATE", "relation": "EXACT_SAME", "target_claim_id": str(exact[0]), "memory_kind": str(exact[2]), "title": str(exact[1]), "content": ""}
    candidate_rows = conn.execute(
        """SELECT id, memory_kind, title, content, content_hash, predicate, subject_text, object_text, status
           FROM claims WHERE subject_id=? AND status='active' AND security_state!='blocked' ORDER BY importance DESC LIMIT 20""",
        (unit["subject_id"],),
    ).fetchall()
    conn.close()
    candidates = [{"id": str(row[0]), "memory_kind": str(row[1]), "title": str(row[2]), "content": str(row[3]), "content_hash": str(row[4]), "predicate": str(row[5] or ""), "subject_text": str(row[6] or ""), "object_text": str(row[7] or ""), "status": str(row[8])} for row in candidate_rows]
    # Node search supplies text/topic candidates that may not match structured
    # predicates; append only unseen claim ids to preserve scope boundaries.
    found = search_nodes(root, str(unit["subject_id"]), str(unit["content"]), limit=20).get("nodes", [])
    known = {item["id"] for item in candidates}
    for item in found:
        if item.get("node_type") == "claim" and str(item.get("node_id")) not in known:
            candidates.append({"id": str(item["node_id"]), "memory_kind": str(item.get("memory_kind", "candidate")), "title": str(item.get("title", "")), "content": str(item.get("summary", "")), "content_hash": "", "predicate": "", "subject_text": "", "object_text": "", "status": str(item.get("status", ""))})
    relation, target = semantic_relation(unit, candidates)
    action = RELATION_TO_ACTION[relation]
    if action == "CORROBORATE" and target:
        return {**base, "action": action, "relation": relation, "target_claim_id": target["id"], "memory_kind": target["memory_kind"], "title": target["title"], "content": ""}
    if action in {"REFINE", "CORRECT", "SUPERSEDE"} and target:
        # Two-source temporal actions cite both the new observation and prior
        # claim evidence. The policy layer will still stage them for approval.
        conn = open_db(root)
        combined_sources = list(dict.fromkeys(_sources(conn, target["id"]) + list(unit["source_event_ids"])))
        conn.close()
        return {**base, "action": action, "relation": relation, "target_claim_id": target["id"], "source_event_ids": combined_sources, "memory_kind": unit["unit_kind"], "verification_state": "verified", "title": unit["topic"], "content": unit["content"], "requires_review": True}
    kind, verification = choose_kind(str(unit["unit_kind"]), float(unit["confidence"]), str(unit["sensitivity"]), policy)
    result = {**base, "action": "CREATE", "relation": relation, "memory_kind": kind, "verification_state": verification, "title": str(unit["topic"]), "content": str(unit["content"])}
    if relation == "CONTRADICTS_UNRESOLVED":
        result["requires_review"] = True
        result["risk_reason"] = "unresolved_structured_conflict"
    return result


def build_plan(root: Path, subject_id: str, *, policy: str = "conservative", limit: int = 20) -> dict[str, object]:
    conn = open_db(root)
    rows = conn.execute(
        """SELECT id, unit_kind, domain, topic, content, content_hash, confidence, uncertainty, importance, durability,
                  sensitivity, source_event_ids, predicate, subject_text, object_text, qualifiers_json, valid_from, valid_to, observed_at, entities_json
           FROM memory_units WHERE subject_id=? AND status='pending' AND security_state!='blocked' ORDER BY id LIMIT ?""",
        (subject_id, max(1, limit)),
    ).fetchall()
    conn.close()
    keys = ["id", "unit_kind", "domain", "topic", "content", "content_hash", "confidence", "uncertainty", "importance", "durability", "sensitivity", "source_event_ids", "predicate", "subject_text", "object_text", "qualifiers_json", "valid_from", "valid_to", "observed_at", "entities_json"]
    actions: list[dict[str, object]] = []
    for row in rows:
        unit = dict(zip(keys, row))
        unit["subject_id"] = subject_id
        unit["source_event_ids"] = json.loads(str(unit.pop("source_event_ids") or "[]"))
        unit["qualifiers"] = json.loads(str(unit.pop("qualifiers_json") or "{}"))
        unit["entities"] = json.loads(str(unit.pop("entities_json") or "[]"))
        actions.append(build_plan_for_unit(root, unit, policy=policy))
    return {"schema_version": 3, "subject_id": subject_id, "policy": policy, "actions": actions}


def main() -> None:
    args = parse_args()
    plan = build_plan(store_root(args.store), args.subject_id, policy=args.policy, limit=args.limit)
    if args.out_file:
        Path(args.out_file).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(plan)


if __name__ == "__main__":
    main()
