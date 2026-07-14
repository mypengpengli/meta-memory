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
from runtime_identity import add_identity_args


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
    add_identity_args(parser)
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


def _compatible_scope(unit: dict[str, object], candidate: dict[str, object]) -> bool:
    left = unit.get("qualifiers") if isinstance(unit.get("qualifiers"), dict) else {}
    right = candidate.get("qualifiers") if isinstance(candidate.get("qualifiers"), dict) else {}
    return not left or not right or left == right


def _has_state_transition(unit: dict[str, object], candidate: dict[str, object]) -> bool:
    text = str(unit.get("content") or "").casefold()
    signal = bool(__import__("re").search(r"\b(now|currently|migrated|changed|replaced)\b|现在|目前|已经|迁移|改成", text))
    newer = str(unit.get("observed_at") or unit.get("valid_from") or "") > str(candidate.get("observed_at") or candidate.get("valid_from") or "")
    return signal and newer and _compatible_scope(unit, candidate)


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
            if (str(unit.get("unit_kind")) == "state" or str(candidate.get("memory_kind")) == "state") and _has_state_transition(unit, candidate):
                return "REPLACES_OLD_STATE", candidate
            return "CONTRADICTS_UNRESOLVED", candidate
        old_tokens, new_tokens = _tokens(str(candidate.get("content") or "")), _tokens(str(unit.get("content") or ""))
        if old_tokens and new_tokens and len(old_tokens & new_tokens) / max(1, min(len(old_tokens), len(new_tokens))) >= 0.7:
            return "ADDS_QUALIFIER", candidate
    return "UNRELATED", None


def _sources(conn, claim_id: str) -> list[int]:
    return [int(row[0]) for row in conn.execute("SELECT raw_event_id FROM claim_sources WHERE claim_id=? ORDER BY raw_event_id", (claim_id,))]


def _normal_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _safe_refinement(unit: dict[str, object], target: dict[str, object], relation: str) -> bool:
    """Allow automatic REFINE only for a strictly additive qualifier update.

    A relation classifier can be helpful but is not authoritative enough to
    rewrite a user memory.  The old structured fact must stay identical and
    its text/qualifiers must be retained verbatim inside the new observation.
    """
    if relation not in {"NARROWS", "ADDS_QUALIFIER"}:
        return False
    if any(
        _normal_text(unit.get(key)) != _normal_text(target.get(key))
        for key in ("predicate", "subject_text", "object_text")
    ):
        return False
    old_content, new_content = _normal_text(target.get("content")), _normal_text(unit.get("content"))
    if not old_content or old_content not in new_content:
        return False
    old_qualifiers = target.get("qualifiers") if isinstance(target.get("qualifiers"), dict) else {}
    new_qualifiers = unit.get("qualifiers") if isinstance(unit.get("qualifiers"), dict) else {}
    return all(key in new_qualifiers and new_qualifiers[key] == value for key, value in old_qualifiers.items())


def build_plan_for_unit(root: Path, unit: dict[str, object], *, policy: str) -> dict[str, object]:
    conn = open_db(root)
    profile, workspace = str(unit.get("profile_id") or "default"), str(unit.get("workspace_id") or "global")
    source_type = str(unit.get("source_type") or "").casefold()
    tool_backed = source_type in {"agent-observation", "tool-result"}
    # API callers predating RuntimeIdentity did not carry a scope; retain
    # their historical profile-wide behavior. Runtime/extractor callers now
    # always supply an explicit workspace or agent visibility.
    visibility, owner = str(unit.get("visibility_scope") or "global"), str(unit.get("owner_agent_id") or "")
    exact_scope = "profile_id=? AND workspace_id=? AND visibility_scope=? AND COALESCE(owner_agent_id,'')=?"
    exact = conn.execute(f"SELECT id, title, memory_kind FROM claims WHERE subject_id=? AND content_hash=? AND status NOT IN ('superseded', 'corrected') AND {exact_scope}", (unit["subject_id"], unit["content_hash"], profile, workspace, visibility, owner)).fetchone()
    base = {"plan_id": str(uuid.uuid4()), "subject_id": unit["subject_id"], "subject_name": str(unit.get("subject_name") or "Unknown"), "unit_id": unit["id"], "source_event_ids": list(unit["source_event_ids"]), "topic": unit["topic"], "domain": unit["domain"], "confidence": round(float(unit["confidence"]), 3), "uncertainty": round(float(unit["uncertainty"]), 3), "importance": round(float(unit["importance"]), 3), "durability": round(float(unit["durability"]), 3), "sensitivity": unit["sensitivity"], "predicate": unit["predicate"], "subject_text": unit["subject_text"], "object_text": unit["object_text"], "qualifiers": unit["qualifiers"], "valid_from": unit["valid_from"], "valid_to": unit["valid_to"], "observed_at": unit["observed_at"], "entities": unit["entities"], "profile_id": profile, "workspace_id": workspace, "visibility_scope": visibility, "owner_agent_id": owner, "origin_agent_id": str(unit.get("origin_agent_id") or "")}
    if source_type == "resource":
        # Imported material is useful as a reviewable project lead, but it is
        # never direct user testimony.  Do not consolidate it into, or
        # corroborate it against, an active factual Claim.
        conn.close()
        return {
            **base,
            "action": "CREATE",
            "relation": "RESOURCE_EVIDENCE",
            "memory_kind": "candidate",
            "verification_state": "resource",
            "title": str(unit["topic"]),
            "content": str(unit["content"]),
            "prompt_eligible": False,
            "resource_candidate": True,
        }
    if tool_backed and exact:
        conn.close()
        if str(exact[2]) == "profile":
            return {**base, "action": "IGNORE", "relation": "PROFILE_PROTECTED", "memory_kind": "profile", "title": str(exact[1]), "content": "", "requires_review": True}
        return {**base, "action": "CORROBORATE", "relation": "EXACT_SAME", "target_claim_id": str(exact[0]), "memory_kind": str(exact[2]), "title": str(exact[1]), "content": "", "verification_state": "agent_observed"}
    if exact:
        conn.close()
        return {**base, "action": "CORROBORATE", "relation": "EXACT_SAME", "target_claim_id": str(exact[0]), "memory_kind": str(exact[2]), "title": str(exact[1]), "content": ""}
    if tool_backed:
        # Evidence from a successful tool or another Agent can be shared as a
        # project observation, but it must never infer a semantic mutation of
        # an existing Claim.  A separately traceable state Claim is safer than
        # letting an LLM relation rewrite user-authored content.
        conn.close()
        kind = str(unit.get("unit_kind") or "state")
        if kind in {"profile", "candidate", "session"}:
            kind = "state"
        return {
            **base,
            "action": "CREATE",
            "relation": "AGENT_OBSERVATION",
            "memory_kind": kind,
            "verification_state": "agent_observed",
            "title": str(unit["topic"]),
            "content": str(unit["content"]),
            "prompt_eligible": True,
        }
    candidate_rows = conn.execute(
        """SELECT id, memory_kind, title, content, content_hash, predicate, subject_text, object_text, status, qualifiers_json, valid_from, observed_at
           FROM claims WHERE subject_id=? AND status='active' AND security_state!='blocked' AND """ + exact_scope + " ORDER BY importance DESC LIMIT 20",
        (unit["subject_id"], profile, workspace, visibility, owner),
    ).fetchall()
    conn.close()
    candidates = [{"id": str(row[0]), "memory_kind": str(row[1]), "title": str(row[2]), "content": str(row[3]), "content_hash": str(row[4]), "predicate": str(row[5] or ""), "subject_text": str(row[6] or ""), "object_text": str(row[7] or ""), "status": str(row[8]), "qualifiers": json.loads(str(row[9] or "{}")), "valid_from": str(row[10] or ""), "observed_at": str(row[11] or "")} for row in candidate_rows]
    # Node search supplies text/topic candidates that may not match structured
    # predicates; append only unseen claim ids to preserve scope boundaries.
    found = search_nodes(root, str(unit["subject_id"]), str(unit["content"]), limit=20, profile_id=profile, workspace_id=workspace, agent_id=str(unit.get("origin_agent_id") or "")).get("nodes", [])
    known = {item["id"] for item in candidates}
    for item in found:
        if item.get("node_type") == "claim" and str(item.get("node_id")) not in known:
            candidates.append({"id": str(item["node_id"]), "memory_kind": str(item.get("memory_kind", "candidate")), "title": str(item.get("title", "")), "content": str(item.get("summary", "")), "content_hash": "", "predicate": "", "subject_text": "", "object_text": "", "status": str(item.get("status", ""))})
    relation, target = semantic_relation(unit, candidates)
    # An optional classifier may suggest a replacement, but only deterministic
    # temporal/scope evidence may permit automatic supersession semantics.
    if relation == "REPLACES_OLD_STATE" and target and not _has_state_transition(unit, target):
        relation = "CONTRADICTS_UNRESOLVED"
    action = RELATION_TO_ACTION[relation]
    if action == "CORROBORATE" and target:
        return {**base, "action": action, "relation": relation, "target_claim_id": target["id"], "memory_kind": target["memory_kind"], "title": target["title"], "content": ""}
    if action in {"REFINE", "CORRECT", "SUPERSEDE"} and target:
        # Two-source temporal actions cite both the new observation and prior
        # claim evidence. The policy layer will still stage them for approval.
        conn = open_db(root)
        combined_sources = list(dict.fromkeys(_sources(conn, target["id"]) + list(unit["source_event_ids"])))
        conn.close()
        return {**base, "action": action, "relation": relation, "target_claim_id": target["id"], "source_event_ids": combined_sources, "memory_kind": unit["unit_kind"], "verification_state": "agent_observed" if tool_backed else "verified", "title": unit["topic"], "content": unit["content"], "requires_review": True, "refine_safe": _safe_refinement(unit, target, relation) if action == "REFINE" else False}
    kind, verification = choose_kind(str(unit["unit_kind"]), float(unit["confidence"]), str(unit["sensitivity"]), policy)
    if tool_backed:
        # A successful tool call is useful project evidence, not a statement
        # about the user's personal profile.  It stays reviewable through the
        # policy gate and carries its distinct provenance when promoted.
        kind = "state" if kind == "profile" else kind
        verification = "agent_observed"
    result = {**base, "action": "CREATE", "relation": relation, "memory_kind": kind, "verification_state": verification, "title": str(unit["topic"]), "content": str(unit["content"])}
    if relation == "CONTRADICTS_UNRESOLVED":
        result["requires_review"] = True
        result["risk_reason"] = "unresolved_structured_conflict"
    return result


def build_plan(root: Path, subject_id: str, *, policy: str = "conservative", limit: int = 20, unit_ids: list[int] | None = None, profile_id: str | None = None, workspace_id: str | None = None, origin_agent_id: str = "") -> dict[str, object]:
    if unit_ids is not None and not unit_ids:
        return {"schema_version": 3, "subject_id": subject_id, "policy": policy, "actions": []}
    conn = open_db(root)
    scope, scope_params = "", []
    if unit_ids:
        scope = " AND id IN ({})".format(", ".join("?" for _ in unit_ids))
        scope_params = list(unit_ids)
    if profile_id is not None:
        scope += " AND profile_id=?"; scope_params.append(profile_id)
    if workspace_id is not None:
        scope += " AND workspace_id=?"; scope_params.append(workspace_id)
    rows = conn.execute(
        """SELECT id, subject_name, unit_kind, domain, topic, content, content_hash, confidence, uncertainty, importance, durability,
                  sensitivity, source_event_ids, source_type, predicate, subject_text, object_text, qualifiers_json, valid_from, valid_to, observed_at, entities_json,
                  profile_id, workspace_id, visibility_scope, origin_agent_id, owner_agent_id
           FROM memory_units WHERE subject_id=? AND status='pending' AND security_state!='blocked'""" + scope + " ORDER BY id LIMIT ?",
        (subject_id, *scope_params, max(1, limit)),
    ).fetchall()
    conn.close()
    keys = ["id", "subject_name", "unit_kind", "domain", "topic", "content", "content_hash", "confidence", "uncertainty", "importance", "durability", "sensitivity", "source_event_ids", "source_type", "predicate", "subject_text", "object_text", "qualifiers_json", "valid_from", "valid_to", "observed_at", "entities_json", "profile_id", "workspace_id", "visibility_scope", "origin_agent_id", "owner_agent_id"]
    actions: list[dict[str, object]] = []
    for row in rows:
        unit = dict(zip(keys, row))
        unit["subject_id"] = subject_id
        unit["source_event_ids"] = json.loads(str(unit.pop("source_event_ids") or "[]"))
        unit["qualifiers"] = json.loads(str(unit.pop("qualifiers_json") or "{}"))
        unit["entities"] = json.loads(str(unit.pop("entities_json") or "[]"))
        action = build_plan_for_unit(root, unit, policy=policy)
        action["source_type"] = str(unit.get("source_type") or "")
        if origin_agent_id:
            action["origin_agent_id"] = origin_agent_id
        actions.append(action)
    return {"schema_version": 3, "subject_id": subject_id, "policy": policy, "actions": actions}


def main() -> None:
    args = parse_args()
    plan = build_plan(store_root(args.store), args.subject_id, policy=args.policy, limit=args.limit, profile_id=args.profile_id, workspace_id=args.workspace_id, origin_agent_id=args.agent_id)
    if args.out_file:
        Path(args.out_file).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(plan)


if __name__ == "__main__":
    main()
