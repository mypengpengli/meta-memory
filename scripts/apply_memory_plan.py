#!/usr/bin/env python3
"""Apply validated plans transactionally; claims are truth and Markdown is projection."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import uuid
from pathlib import Path

from _common import DEFAULT_STORE_HELP, compose_markdown, emit, open_db, sha256_text, store_root, utc_now
from proposal_manager import stage_memory_proposal
from security_scan import findings_json, scan_memory_content, security_state
from validate_memory_plan import load_plan, validate_plan
from write_memory import KIND_DIRS, slugify


HIGH_RISK = {"CORRECT", "SUPERSEDE"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply a validated memory plan atomically and audit every action.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--plan-file", required=True)
    parser.add_argument("--review-approved", action="store_true")
    parser.add_argument("--skip-index", action="store_true")
    return parser.parse_args()


def action_sources(action: dict[str, object]) -> list[int]:
    return list(dict.fromkeys(int(value) for value in action.get("source_event_ids", []) if str(value).isdigit()))


def claim_path(root: Path, kind: str, claim_id: str) -> Path:
    path = root / KIND_DIRS.get(kind, "candidates") / f"claim-{slugify(claim_id)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def claim_markdown(claim: dict[str, object], source_ids: list[int]) -> str:
    meta = {
        "schema_version": 3, "memory_id": claim["id"], "subject_id": claim["subject_id"], "subject_name": claim["subject_name"],
        "memory_kind": claim["memory_kind"], "page_role": "claim", "canonical": False, "domain": claim.get("domain", "general"),
        "topic": claim["topic"], "tags": ["claim", claim["verification_state"]], "valid_from": claim.get("valid_from", ""), "valid_to": claim.get("valid_to", ""),
        "observed_at": claim.get("observed_at", ""), "predicate": claim.get("predicate", ""), "subject_text": claim.get("subject_text", ""), "object_text": claim.get("object_text", ""),
        "qualifiers": claim.get("qualifiers", {}), "confidence": claim["confidence"], "importance": claim["importance"], "durability": claim.get("durability", 0.5),
        "status": claim["status"], "verification_state": claim["verification_state"], "sensitivity": claim["sensitivity"], "security_state": claim.get("security_state", "clean"),
        "prompt_eligible": bool(claim.get("prompt_eligible", 1)), "source_event_ids": source_ids, "source": ", ".join(f"raw_event:{value}" for value in source_ids),
        "related_people": [], "related_events": [], "related_topics": [claim["topic"]], "related_sources": [f"raw_event:{value}" for value in source_ids],
        "supersedes": [claim["supersedes"]] if claim.get("supersedes") else [], "replaced_by": [claim["replaced_by"]] if claim.get("replaced_by") else [], "corrected_by": [claim["corrected_by"]] if claim.get("corrected_by") else [],
    }
    validity = str(claim.get("valid_to") or "") or "仍有效"
    body = "\n".join([f"# {claim['title']}", "", "## Claim", "", str(claim["content"]).strip(), "", "## Structured meaning", "", f"- {claim.get('subject_text', '')} — {claim.get('predicate', '')} → {claim.get('object_text', '')}", "", "## Time boundary", "", f"- Valid from: {claim.get('valid_from') or '未指定'}", f"- Valid to: {validity}", "", "## Evidence", "", *[f"- raw_event:{value}" for value in source_ids]])
    return compose_markdown(meta, body)


def atomic_replace(path: Path, content: str) -> tuple[str | None, str]:
    old = path.read_text(encoding="utf-8") if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
    return old, sha256_text(content)


def restore(path: Path, old: str | None) -> None:
    if old is None:
        path.unlink(missing_ok=True)
    else:
        atomic_replace(path, old)


def fetch_claim(conn, claim_id: str) -> dict[str, object]:
    columns = "id, subject_id, subject_name, memory_kind, domain, topic, title, content, status, verification_state, confidence, importance, sensitivity, valid_from, valid_to, observed_at, support_count, memory_path, predicate, subject_text, object_text, qualifiers_json, durability, confirmed_utility, replaced_by, corrected_by, supersedes, security_state, security_findings_json, prompt_eligible"
    row = conn.execute(f"SELECT {columns} FROM claims WHERE id=?", (claim_id,)).fetchone()
    if row is None:
        raise ValueError(f"Claim not found: {claim_id}")
    keys = columns.split(", ")
    claim = dict(zip(keys, row))
    claim["qualifiers"] = json.loads(str(claim.pop("qualifiers_json") or "{}"))
    return claim


def all_sources(conn, claim_id: str) -> list[int]:
    return [int(row[0]) for row in conn.execute("SELECT raw_event_id FROM claim_sources WHERE claim_id=? ORDER BY raw_event_id", (claim_id,))]


def link_sources(conn, claim_id: str, ids: list[int]) -> None:
    for event_id in ids:
        conn.execute("INSERT OR IGNORE INTO claim_sources(claim_id, raw_event_id, source_role) VALUES(?, ?, 'supports')", (claim_id, event_id))


def save_claim_file(root: Path, conn, claim: dict[str, object], source_ids: list[int], reason: str) -> tuple[Path, str | None]:
    raw = str(claim.get("memory_path") or "")
    path = Path(raw) if raw else claim_path(root, str(claim["memory_kind"]), str(claim["id"]))
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("Claim path escaped memory-data") from exc
    old, _ = atomic_replace(path, claim_markdown(claim, source_ids))
    if old is not None:
        conn.execute("INSERT INTO memory_versions(memory_path, content, content_hash, reason) VALUES(?, ?, ?, ?)", (str(path), old, sha256_text(old), reason))
    conn.execute("UPDATE claims SET memory_path=?, updated_at=? WHERE id=?", (str(path), utc_now(), claim["id"]))
    claim["memory_path"] = str(path)
    return path, old


def create_claim(conn, root: Path, action: dict[str, object], *, replacement_of: str = "", edge_type: str = "") -> tuple[str, Path, str | None]:
    claim_id, ids = str(uuid.uuid4()), action_sources(action)
    content = str(action["content"])
    findings = scan_memory_content(content, source_type="memory_plan")
    state, eligible = security_state(findings)
    claim = {"id": claim_id, "subject_id": action["subject_id"], "subject_name": action.get("subject_name", "Unknown"), "memory_kind": action.get("memory_kind", "candidate"), "domain": action.get("domain", "general"), "topic": action.get("topic", "memory"), "title": action.get("title", action.get("topic", "Memory claim")), "content": content, "status": "active" if action.get("memory_kind") != "candidate" else "candidate", "verification_state": action.get("verification_state", "unverified"), "confidence": float(action.get("confidence", 0.3)), "importance": float(action.get("importance", 0.3)), "durability": float(action.get("durability", 0.5)), "sensitivity": action.get("sensitivity", "normal"), "valid_from": action.get("valid_from") or utc_now(), "valid_to": action.get("valid_to", ""), "observed_at": action.get("observed_at") or utc_now(), "support_count": len(ids), "memory_path": "", "predicate": action.get("predicate", "states"), "subject_text": action.get("subject_text", "user"), "object_text": action.get("object_text", content[:240]), "qualifiers": action.get("qualifiers", {}), "confirmed_utility": 0.0, "replaced_by": "", "corrected_by": "", "supersedes": replacement_of, "security_state": state, "prompt_eligible": eligible}
    conn.execute("""INSERT INTO claims(id, subject_id, subject_name, memory_kind, domain, topic, title, content, content_hash, status, verification_state, confidence, importance, sensitivity, valid_from, valid_to, observed_at, support_count, memory_path, predicate, subject_text, object_text, qualifiers_json, durability, confirmed_utility, replaced_by, corrected_by, supersedes, security_state, security_findings_json, prompt_eligible, source_unit_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (claim_id, claim["subject_id"], claim["subject_name"], claim["memory_kind"], claim["domain"], claim["topic"], claim["title"], content, sha256_text(content), claim["status"], claim["verification_state"], claim["confidence"], claim["importance"], claim["sensitivity"], claim["valid_from"], claim["valid_to"], claim["observed_at"], claim["support_count"], "", claim["predicate"], claim["subject_text"], claim["object_text"], json.dumps(claim["qualifiers"], ensure_ascii=False), claim["durability"], 0.0, "", "", replacement_of, state, json.dumps(findings_json(findings), ensure_ascii=False), eligible, action.get("unit_id")))
    link_sources(conn, claim_id, ids)
    path, old = save_claim_file(root, conn, claim, ids, "create")
    if replacement_of and edge_type:
        conn.execute("INSERT OR IGNORE INTO memory_edges(subject_id, from_claim_id, to_claim_id, edge_type, source_event_ids) VALUES(?, ?, ?, ?, ?)", (claim["subject_id"], replacement_of, claim_id, edge_type, json.dumps(ids)))
    return claim_id, path, old


def queue_review(conn, action: dict[str, object], reason: str) -> str:
    uid = stage_memory_proposal(conn, action, origin=str(action.get("origin", "background_review")), reason=reason)
    conn.execute("INSERT OR IGNORE INTO review_queue(plan_id, subject_id, reason, payload) VALUES(?, ?, ?, ?)", (action["plan_id"], action["subject_id"], reason, json.dumps(action, ensure_ascii=False)))
    conn.execute("INSERT OR IGNORE INTO consolidation_runs(plan_id, subject_id, policy, action, status, payload) VALUES(?, ?, ?, ?, 'review', ?)", (action["plan_id"], action["subject_id"], action.get("policy", "conservative"), action["action"], json.dumps(action, ensure_ascii=False)))
    return uid


def apply_action(root: Path, action: dict[str, object], policy: str, review_approved: bool) -> dict[str, object]:
    conn = open_db(root)
    previous = conn.execute("SELECT status FROM consolidation_runs WHERE plan_id=?", (action["plan_id"],)).fetchone()
    if previous and str(previous[0]) == "applied":
        conn.close()
        return {"plan_id": action["plan_id"], "status": "skipped", "reason": "already_applied"}
    name = str(action["action"]).upper()
    must_review = name in HIGH_RISK or bool(action.get("requires_review")) or (name == "CREATE" and str(action.get("verification_state")) == "verified" and str(action.get("origin", "")) == "background_review")
    if must_review and not review_approved:
        proposal_id = queue_review(conn, action, "high_risk_or_policy_review")
        conn.commit(); conn.close()
        return {"plan_id": action["plan_id"], "status": "review", "proposal_id": proposal_id}
    backups: list[tuple[Path, str | None]] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        result: dict[str, object] = {"plan_id": action["plan_id"], "action": name}
        if name == "IGNORE":
            if action.get("unit_id"):
                conn.execute("UPDATE memory_units SET status='ignored', updated_at=? WHERE id=?", (utc_now(), action["unit_id"]))
        elif name == "CREATE":
            cid, path, old = create_claim(conn, root, action); backups.append((path, old)); result.update({"claim_id": cid, "path": str(path)})
        elif name == "CORROBORATE":
            claim = fetch_claim(conn, str(action["target_claim_id"])); link_sources(conn, str(claim["id"]), action_sources(action)); ids = all_sources(conn, str(claim["id"])); claim["support_count"], claim["confidence"], claim["observed_at"] = len(ids), min(1.0, max(float(claim["confidence"]), float(action.get("confidence", 0))) + 0.03), utc_now()
            conn.execute("UPDATE claims SET support_count=?, confidence=?, observed_at=?, updated_at=? WHERE id=?", (claim["support_count"], claim["confidence"], claim["observed_at"], utc_now(), claim["id"])); path, old = save_claim_file(root, conn, claim, ids, "corroborate"); backups.append((path, old)); result.update({"claim_id": claim["id"], "path": str(path)})
        elif name == "REFINE":
            claim = fetch_claim(conn, str(action["target_claim_id"])); claim["content"] = str(action.get("content") or claim["content"]); claim["topic"] = str(action.get("topic") or claim["topic"]); claim["domain"] = str(action.get("domain") or claim["domain"]); claim["predicate"] = str(action.get("predicate") or claim["predicate"]); claim["object_text"] = str(action.get("object_text") or claim["object_text"]); claim["confidence"] = max(float(claim["confidence"]), float(action.get("confidence", 0))); link_sources(conn, str(claim["id"]), action_sources(action)); ids = all_sources(conn, str(claim["id"])); claim["support_count"] = len(ids)
            conn.execute("UPDATE claims SET content=?, content_hash=?, topic=?, domain=?, predicate=?, object_text=?, confidence=?, support_count=?, observed_at=?, updated_at=? WHERE id=?", (claim["content"], sha256_text(str(claim["content"])), claim["topic"], claim["domain"], claim["predicate"], claim["object_text"], claim["confidence"], len(ids), utc_now(), utc_now(), claim["id"])); path, old = save_claim_file(root, conn, claim, ids, "refine"); backups.append((path, old)); result.update({"claim_id": claim["id"], "path": str(path)})
        elif name in HIGH_RISK:
            old_claim = fetch_claim(conn, str(action["target_claim_id"])); replacement = dict(action); replacement.setdefault("memory_kind", old_claim["memory_kind"]); replacement.setdefault("domain", old_claim["domain"]); replacement.setdefault("topic", old_claim["topic"]); replacement.setdefault("title", old_claim["title"]); replacement.setdefault("verification_state", "verified")
            new_id, path, old = create_claim(conn, root, replacement, replacement_of=str(old_claim["id"]), edge_type="corrects" if name == "CORRECT" else "supersedes"); backups.append((path, old))
            if name == "SUPERSEDE":
                old_claim["status"], old_claim["valid_to"], old_claim["replaced_by"] = "superseded", str(replacement.get("valid_from") or utc_now()), new_id
                conn.execute("UPDATE claims SET status='superseded', valid_to=?, replaced_by=?, updated_at=? WHERE id=?", (old_claim["valid_to"], new_id, utc_now(), old_claim["id"]))
            else:
                old_claim["status"], old_claim["verification_state"], old_claim["corrected_by"] = "corrected", "invalid", new_id
                conn.execute("UPDATE claims SET status='corrected', verification_state='invalid', corrected_by=?, updated_at=? WHERE id=?", (new_id, utc_now(), old_claim["id"]))
            old_path, old_backup = save_claim_file(root, conn, old_claim, all_sources(conn, str(old_claim["id"])), name.casefold()); backups.append((old_path, old_backup)); result.update({"claim_id": new_id, "path": str(path), "replaces": old_claim["id"]})
        if action.get("unit_id"):
            conn.execute("UPDATE memory_units SET status='consolidated', updated_at=? WHERE id=?", (utc_now(), action["unit_id"]))
        conn.execute("INSERT INTO consolidation_runs(plan_id, subject_id, policy, action, status, payload, applied_at) VALUES(?, ?, ?, ?, 'applied', ?, ?) ON CONFLICT(plan_id) DO UPDATE SET status='applied', applied_at=excluded.applied_at, error=NULL", (action["plan_id"], action["subject_id"], policy, name, json.dumps(action, ensure_ascii=False), utc_now()))
        conn.commit(); conn.close()
        return {**result, "status": "applied"}
    except Exception:
        conn.rollback()
        for path, old in reversed(backups): restore(path, old)
        conn.execute("INSERT OR REPLACE INTO consolidation_runs(plan_id, subject_id, policy, action, status, payload, error) VALUES(?, ?, ?, ?, 'failed', ?, ?)", (action.get("plan_id", str(uuid.uuid4())), action.get("subject_id", ""), policy, name, json.dumps(action, ensure_ascii=False), "apply_failed")); conn.commit(); conn.close(); raise


def apply_plan(root: Path, plan: dict[str, object], *, review_approved: bool = False, skip_index: bool = False) -> dict[str, object]:
    validation = validate_plan(root, plan)
    if not validation["valid"]:
        return {"status": "invalid", "validation": validation, "results": []}
    results = [apply_action(root, dict(action), str(plan.get("policy", "conservative")), review_approved) for action in plan["actions"]]
    from entity_resolution import resolve_claim_entities
    for action, result in zip(plan["actions"], results):
        if result.get("status") == "applied" and result.get("claim_id"):
            resolve_claim_entities(root, str(result["claim_id"]), list(action.get("entities") or []))
    indexing: list[dict[str, object]] = []
    if not skip_index and any(item["status"] == "applied" for item in results):
        from write_memory import run_indexing
        indexing = run_indexing(root)
        from build_hot_memory import build_hot_memory
        for subject_id in {str(action["subject_id"]) for action in plan["actions"]}:
            build_hot_memory(root, subject_id=subject_id)
    return {"status": "ok", "validation": validation, "results": results, "indexing": indexing}


def main() -> None:
    args = parse_args()
    emit(apply_plan(store_root(args.store), load_plan(args.plan_file), review_approved=args.review_approved, skip_index=args.skip_index))


if __name__ == "__main__":
    main()
