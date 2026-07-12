#!/usr/bin/env python3
"""Apply only validated plans, with database audit records and atomic Markdown writes."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from _common import DEFAULT_STORE_HELP, compose_markdown, emit, open_db, sha256_text, store_root
from validate_memory_plan import load_plan, validate_plan
from write_memory import KIND_DIRS, slugify


HIGH_RISK = {"CORRECT", "SUPERSEDE"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply a validated memory plan atomically and audit every action.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--plan-file", required=True)
    parser.add_argument("--review-approved", action="store_true", help="Apply high-risk corrections after an explicit review")
    parser.add_argument("--skip-index", action="store_true")
    return parser.parse_args()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def action_sources(action: dict[str, object]) -> list[int]:
    return list(dict.fromkeys(int(value) for value in action.get("source_event_ids", []) if str(value).isdigit()))


def claim_path(root: Path, kind: str, claim_id: str) -> Path:
    folder = root / KIND_DIRS.get(kind, "candidates")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"claim-{slugify(claim_id)}.md"


def claim_markdown(claim: dict[str, object], source_ids: list[int]) -> str:
    meta = {
        "schema_version": 2,
        "memory_id": claim["id"],
        "subject_id": claim["subject_id"],
        "subject_name": claim["subject_name"],
        "memory_kind": claim["memory_kind"],
        "page_role": "claim",
        "canonical": False,
        "domain": "general",
        "topic": claim["topic"],
        "tags": ["claim", claim["verification_state"]],
        "valid_from": claim.get("valid_from", ""),
        "valid_to": claim.get("valid_to", ""),
        "confidence": claim["confidence"],
        "importance": claim["importance"],
        "status": claim["status"],
        "verification_state": claim["verification_state"],
        "sensitivity": claim["sensitivity"],
        "source_event_ids": source_ids,
        "source": ", ".join(f"raw_event:{value}" for value in source_ids),
        "related_people": [],
        "related_events": [],
        "related_topics": [claim["topic"]],
        "related_sources": [f"raw_event:{value}" for value in source_ids],
        "supersedes": [],
        "replaced_by": [],
    }
    valid_to = str(claim.get("valid_to") or "") or "仍有效"
    body = "\n".join(
        [
            f"# {claim['title']}",
            "",
            "## 当前认知",
            "",
            str(claim["content"]).strip(),
            "",
            "## 时间边界",
            "",
            f"- 生效：{claim.get('valid_from') or '未指定'}",
            f"- 失效：{valid_to}",
            "",
            "## 证据",
            "",
            *[f"- raw_event:{value}" for value in source_ids],
        ]
    )
    return compose_markdown(meta, body)


def atomic_replace(path: Path, content: str) -> tuple[str | None, str]:
    old = path.read_text(encoding="utf-8") if path.exists() else None
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return old, sha256_text(content)


def restore(path: Path, old: str | None) -> None:
    if old is None:
        path.unlink(missing_ok=True)
    else:
        atomic_replace(path, old)


def fetch_claim(conn, claim_id: str) -> dict[str, object]:
    row = conn.execute(
        """SELECT id, subject_id, subject_name, memory_kind, topic, title, content, status, verification_state,
                  confidence, importance, sensitivity, valid_from, valid_to, observed_at, support_count, memory_path
           FROM claims WHERE id=?""",
        (claim_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Claim not found: {claim_id}")
    keys = ["id", "subject_id", "subject_name", "memory_kind", "topic", "title", "content", "status", "verification_state", "confidence", "importance", "sensitivity", "valid_from", "valid_to", "observed_at", "support_count", "memory_path"]
    return dict(zip(keys, row))


def save_claim_file(root: Path, conn, claim: dict[str, object], source_ids: list[int], reason: str) -> tuple[Path, str | None]:
    raw_path = str(claim.get("memory_path") or "").strip()
    path = Path(raw_path) if raw_path else None
    if path is None:
        path = claim_path(root, str(claim["memory_kind"]), str(claim["id"]))
        claim["memory_path"] = str(path)
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("Claim path escaped memory-data") from exc
    old, digest = atomic_replace(path, claim_markdown(claim, source_ids))
    if old is not None:
        conn.execute("INSERT INTO memory_versions(memory_path, content, content_hash, reason) VALUES(?, ?, ?, ?)", (str(path), old, sha256_text(old), reason))
    conn.execute("UPDATE claims SET memory_path=?, updated_at=? WHERE id=?", (str(path), now(), claim["id"]))
    return path, old


def link_sources(conn, claim_id: str, ids: list[int]) -> None:
    for event_id in ids:
        conn.execute("INSERT OR IGNORE INTO claim_sources(claim_id, raw_event_id, source_role) VALUES(?, ?, 'supports')", (claim_id, event_id))


def all_sources(conn, claim_id: str) -> list[int]:
    return [int(row[0]) for row in conn.execute("SELECT raw_event_id FROM claim_sources WHERE claim_id=? ORDER BY raw_event_id", (claim_id,))]


def create_claim(conn, root: Path, action: dict[str, object], *, replacement_of: str | None = None, edge_type: str | None = None) -> tuple[str, Path, str | None]:
    claim_id = str(uuid.uuid4())
    ids = action_sources(action)
    claim = {
        "id": claim_id,
        "subject_id": action["subject_id"],
        "subject_name": action.get("subject_name", "Unknown"),
        "memory_kind": action.get("memory_kind", "candidate"),
        "topic": action.get("topic", "memory"),
        "title": action.get("title", action.get("topic", "Memory claim")),
        "content": action["content"],
        "status": "active" if action.get("memory_kind") != "candidate" else "candidate",
        "verification_state": action.get("verification_state", "unverified"),
        "confidence": float(action.get("confidence", 0.3)),
        "importance": float(action.get("importance", 0.3)),
        "sensitivity": action.get("sensitivity", "normal"),
        "valid_from": action.get("valid_from", "") or now(),
        "valid_to": action.get("valid_to", ""),
        "observed_at": now(),
        "support_count": len(ids),
        "memory_path": "",
    }
    conn.execute(
        """
        INSERT INTO claims(id, subject_id, subject_name, memory_kind, topic, title, content, content_hash, status,
        verification_state, confidence, importance, sensitivity, valid_from, valid_to, observed_at, support_count, source_unit_id)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (claim_id, claim["subject_id"], claim["subject_name"], claim["memory_kind"], claim["topic"], claim["title"], claim["content"], sha256_text(str(claim["content"])), claim["status"], claim["verification_state"], claim["confidence"], claim["importance"], claim["sensitivity"], claim["valid_from"], claim["valid_to"], claim["observed_at"], claim["support_count"], action.get("unit_id")),
    )
    link_sources(conn, claim_id, ids)
    path, old = save_claim_file(root, conn, claim, ids, "create")
    if replacement_of and edge_type:
        conn.execute("INSERT OR IGNORE INTO memory_edges(subject_id, from_claim_id, to_claim_id, edge_type, source_event_ids) VALUES(?, ?, ?, ?, ?)", (claim["subject_id"], replacement_of, claim_id, edge_type, json.dumps(ids)))
    return claim_id, path, old


def queue_review(conn, action: dict[str, object], reason: str) -> None:
    conn.execute("INSERT OR IGNORE INTO review_queue(plan_id, subject_id, reason, payload) VALUES(?, ?, ?, ?)", (action["plan_id"], action["subject_id"], reason, json.dumps(action, ensure_ascii=False)))
    conn.execute("INSERT OR IGNORE INTO consolidation_runs(plan_id, subject_id, policy, action, status, payload) VALUES(?, ?, ?, ?, 'review', ?)", (action["plan_id"], action["subject_id"], action.get("policy", "conservative"), action["action"], json.dumps(action, ensure_ascii=False)))


def apply_action(root: Path, action: dict[str, object], policy: str, review_approved: bool) -> dict[str, object]:
    conn = open_db(root)
    existing_run = conn.execute("SELECT status FROM consolidation_runs WHERE plan_id=?", (action["plan_id"],)).fetchone()
    if existing_run and str(existing_run[0]) == "applied":
        conn.close()
        return {"plan_id": action["plan_id"], "status": "skipped", "reason": "already_applied"}
    action_name = str(action["action"]).upper()
    if action_name in HIGH_RISK and not review_approved:
        queue_review(conn, action, "high_risk_temporal_change")
        conn.commit()
        conn.close()
        return {"plan_id": action["plan_id"], "status": "review", "reason": "high_risk_temporal_change"}
    backups: list[tuple[Path, str | None]] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        result: dict[str, object] = {"plan_id": action["plan_id"], "action": action_name}
        if action_name == "IGNORE":
            if action.get("unit_id"):
                conn.execute("UPDATE memory_units SET status='ignored', updated_at=? WHERE id=?", (now(), action["unit_id"]))
        elif action_name == "CREATE":
            claim_id, path, old = create_claim(conn, root, action)
            backups.append((path, old))
            result.update({"claim_id": claim_id, "path": str(path)})
        elif action_name == "CORROBORATE":
            claim = fetch_claim(conn, str(action["target_claim_id"]))
            link_sources(conn, str(claim["id"]), action_sources(action))
            ids = all_sources(conn, str(claim["id"]))
            claim["support_count"] = len(ids)
            claim["confidence"] = min(1.0, max(float(claim["confidence"]), float(action.get("confidence", 0))) + 0.03)
            conn.execute("UPDATE claims SET support_count=?, confidence=?, observed_at=?, updated_at=? WHERE id=?", (claim["support_count"], claim["confidence"], now(), now(), claim["id"]))
            path, old = save_claim_file(root, conn, claim, ids, "corroborate")
            backups.append((path, old))
            result.update({"claim_id": claim["id"], "path": str(path)})
        elif action_name == "REFINE":
            claim = fetch_claim(conn, str(action["target_claim_id"]))
            claim["content"] = str(action.get("content") or claim["content"])
            requested_kind = str(action.get("memory_kind") or claim["memory_kind"])
            if requested_kind in KIND_DIRS:
                claim["memory_kind"] = requested_kind
            if claim["memory_kind"] != "candidate" and str(action.get("verification_state", "")) == "verified":
                claim["status"] = "active"
                claim["verification_state"] = "verified"
            claim["confidence"] = max(float(claim["confidence"]), float(action.get("confidence", 0)))
            link_sources(conn, str(claim["id"]), action_sources(action))
            ids = all_sources(conn, str(claim["id"]))
            claim["support_count"] = len(ids)
            conn.execute("UPDATE claims SET memory_kind=?, status=?, verification_state=?, content=?, content_hash=?, confidence=?, support_count=?, observed_at=?, updated_at=? WHERE id=?", (claim["memory_kind"], claim["status"], claim["verification_state"], claim["content"], sha256_text(str(claim["content"])), claim["confidence"], len(ids), now(), now(), claim["id"]))
            path, old = save_claim_file(root, conn, claim, ids, "refine")
            backups.append((path, old))
            result.update({"claim_id": claim["id"], "path": str(path)})
        elif action_name in HIGH_RISK:
            old_claim = fetch_claim(conn, str(action["target_claim_id"]))
            status = "corrected" if action_name == "CORRECT" else "superseded"
            conn.execute("UPDATE claims SET status=?, valid_to=?, updated_at=? WHERE id=?", (status, now(), now(), old_claim["id"]))
            replacement = dict(action)
            replacement.setdefault("memory_kind", old_claim["memory_kind"])
            replacement.setdefault("topic", old_claim["topic"])
            replacement.setdefault("title", old_claim["title"])
            replacement.setdefault("verification_state", "verified")
            claim_id, path, old = create_claim(conn, root, replacement, replacement_of=str(old_claim["id"]), edge_type="corrects" if action_name == "CORRECT" else "supersedes")
            backups.append((path, old))
            result.update({"claim_id": claim_id, "path": str(path), "replaces": old_claim["id"]})
        if action.get("unit_id"):
            conn.execute("UPDATE memory_units SET status='consolidated', updated_at=? WHERE id=?", (now(), action["unit_id"]))
        conn.execute(
            """INSERT INTO consolidation_runs(plan_id, subject_id, policy, action, status, payload, applied_at)
               VALUES(?, ?, ?, ?, 'applied', ?, ?)
               ON CONFLICT(plan_id) DO UPDATE SET status='applied', applied_at=excluded.applied_at, error=NULL""",
            (action["plan_id"], action["subject_id"], policy, action_name, json.dumps(action, ensure_ascii=False), now()),
        )
        conn.commit()
        conn.close()
        return {**result, "status": "applied"}
    except Exception as exc:
        conn.rollback()
        for path, old in reversed(backups):
            restore(path, old)
        conn.execute("INSERT OR REPLACE INTO consolidation_runs(plan_id, subject_id, policy, action, status, payload, error) VALUES(?, ?, ?, ?, 'failed', ?, ?)", (action.get("plan_id", str(uuid.uuid4())), action.get("subject_id", ""), policy, action_name, json.dumps(action, ensure_ascii=False), str(exc)))
        conn.commit()
        conn.close()
        raise


def apply_plan(root: Path, plan: dict[str, object], *, review_approved: bool = False, skip_index: bool = False) -> dict[str, object]:
    report = validate_plan(root, plan)
    if not report["valid"]:
        return {"status": "invalid", "validation": report, "results": []}
    policy = str(plan.get("policy", "conservative"))
    results = [apply_action(root, dict(action), policy, review_approved) for action in plan["actions"]]
    indexing: list[dict[str, object]] = []
    if not skip_index and any(row["status"] == "applied" for row in results):
        from write_memory import run_indexing

        indexing = run_indexing(root)
    return {"status": "ok", "validation": report, "results": results, "indexing": indexing}


def main() -> None:
    args = parse_args()
    emit(apply_plan(store_root(args.store), load_plan(args.plan_file), review_approved=args.review_approved, skip_index=args.skip_index))


if __name__ == "__main__":
    main()
