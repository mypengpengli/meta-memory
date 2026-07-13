#!/usr/bin/env python3
"""Scope-isolated, immutable-in-session hot-memory projections."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, sha256_text, store_root, utc_now
from config import get
from security_scan import sanitize_for_context
from runtime_identity import identity_from, visibility_sql
from write_memory import slugify


TYPE_PRIORITY = {"profile": 1.0, "state": 0.9, "goal": 0.85, "relationship": 0.75, "domain": 0.30, "event": 0.2}
AGENT_PREDICATES = {"procedure", "operating_principle", "response_constraint", "workflow_preference"}


def hot_scope_dir(root: Path, *, workspace_id: str, profile_id: str, subject_id: str, agent_id: str = "") -> Path:
    return root / "hot" / slugify(workspace_id) / slugify(profile_id) / sha256_text(subject_id)[:16] / (slugify(agent_id) if agent_id else "shared")


def _number(value: object, default: float = 0.0) -> float:
    try: return float(value)
    except (TypeError, ValueError): return default


def hot_memory_score(claim: dict[str, object]) -> float:
    text = str(claim["content"])
    score = _number(claim.get("importance")) * .25 + _number(claim.get("confidence")) * .20
    score += max(-1., min(1., _number(claim.get("confirmed_utility")))) * .15 + _number(claim.get("durability"), .5) * .15
    score += TYPE_PRIORITY.get(str(claim.get("memory_kind")), .1) * .15 + (.10 if not str(claim.get("valid_from") or "") or str(claim.get("valid_from")) <= utc_now() else 0)
    score -= min(.18, len(text) / 12000) + _number(claim.get("uncertainty")) * .12
    if str(claim.get("sensitivity")) == "sensitive": score -= .35
    return round(score, 6)


def _eligible_claims(root: Path, subject_id: str, *, profile_id: str, workspace_id: str, agent_id: str = "") -> list[dict[str, object]]:
    conn = open_db(root)
    scope_sql, scope_params = visibility_sql(identity_from(profile_id=profile_id, workspace_id=workspace_id, agent_id=agent_id), alias="c")
    rows = conn.execute("""SELECT c.id, c.memory_kind, c.domain, c.topic, c.title, c.content, c.predicate,
        c.confidence, c.importance, c.sensitivity, c.valid_from, c.valid_to, c.durability, c.confirmed_utility,
        (SELECT COUNT(*) FROM claim_sources cs WHERE cs.claim_id=c.id) FROM claims c
        WHERE c.subject_id=? AND """ + scope_sql + """ AND c.status='active' AND c.prompt_eligible=1 AND c.security_state IN ('clean','reviewed_safe')
          AND c.verification_state NOT IN ('unverified','disputed','invalid')
          AND (c.valid_from IS NULL OR c.valid_from='' OR c.valid_from<=?)
          AND (c.valid_to IS NULL OR c.valid_to='' OR c.valid_to>?) AND (c.replaced_by IS NULL OR c.replaced_by='')""", (subject_id, *scope_params, utc_now(), utc_now())).fetchall()
    conn.close()
    keys = ["id","memory_kind","domain","topic","title","content","predicate","confidence","importance","sensitivity","valid_from","valid_to","durability","confirmed_utility","sources"]
    return [dict(zip(keys, row)) for row in rows if int(row[-1] or 0)]


def _render(title: str, items: list[dict[str, object]], budget: int) -> tuple[str, list[str]]:
    lines, ids = [f"# {title}", "", "Generated projection; edit claims, never this file."], []
    for item in items:
        line = f"- {sanitize_for_context(str(item['content']).strip())}  <!-- claim:{item['id']} -->"
        if len("\n".join(lines + [line])) + 1 > budget: continue
        lines.append(line); ids.append(str(item["id"]))
    if not ids: lines.append("- No eligible memory is currently available.")
    return "\n".join(lines).rstrip() + "\n", ids


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
    except Exception:
        Path(temp).unlink(missing_ok=True); raise


def _content_hash(parts: dict[str, str]) -> str:
    return hashlib.sha256("".join(parts[name] for name in ("USER.md","AGENT.md","CURRENT.md")).encode("utf-8")).hexdigest()


def build_hot_memory(root: Path, *, subject_id: str, profile_id: str = "default", workspace_id: str = "default", agent_id: str = "", force: bool = False) -> dict[str, object]:
    claims = _eligible_claims(root, subject_id, profile_id=profile_id, workspace_id=workspace_id, agent_id=agent_id)
    for claim in claims: claim["score"] = hot_memory_score(claim)
    claims.sort(key=lambda item: (float(item["score"]), float(item["importance"])), reverse=True)
    quotas, selected = {"profile": int(get("hot_memory.quotas.profile")), "state": int(get("hot_memory.quotas.state")), "goal": int(get("hot_memory.quotas.goal")), "relationship": int(get("hot_memory.quotas.relationship")), "domain": int(get("hot_memory.quotas.domain"))}, []
    counts: dict[str,int] = {}
    for claim in claims:
        kind = str(claim["memory_kind"])
        if counts.get(kind,0) < quotas.get(kind,1): counts[kind] = counts.get(kind,0)+1; selected.append(claim)
    user = [item for item in selected if item["memory_kind"] in {"profile","relationship"}]
    current = [item for item in selected if item["memory_kind"] in {"state","goal","event"}]
    agent = [item for item in selected if str(item.get("predicate") or "") in AGENT_PREDICATES]
    rendered = {"USER.md": _render("Core User Memory", user, int(get("hot_memory.user_max_chars"))), "AGENT.md": _render("Agent Operating Principles", agent, int(get("hot_memory.agent_max_chars"))), "CURRENT.md": _render("Current Priorities", current, int(get("hot_memory.current_max_chars")))}
    texts = {name: pair[0] for name, pair in rendered.items()}; ids = [claim_id for _, claim_ids in rendered.values() for claim_id in claim_ids]; digest = _content_hash(texts)
    conn = open_db(root)
    previous = conn.execute("SELECT snapshot_uid, content_hash FROM hot_snapshots WHERE workspace_id=? AND profile_id=? AND subject_id=? AND agent_id=? AND session_id='' ORDER BY created_at DESC LIMIT 1", (workspace_id, profile_id, subject_id, agent_id)).fetchone()
    if previous and str(previous[1]) == digest and not force:
        conn.execute("UPDATE hot_snapshots SET last_checked_at=? WHERE snapshot_uid=?", (utc_now(), previous[0])); conn.commit(); conn.close()
        return {"snapshot_uid": str(previous[0]), "content_hash": digest, "changed": False, "scope": str(hot_scope_dir(root, workspace_id=workspace_id, profile_id=profile_id, subject_id=subject_id, agent_id=agent_id)), "source_claim_ids": ids}
    # The canonical snapshot is mutable and unique per scope. Frozen session
    # snapshots are copied into their own rows, so this never changes a
    # conversation that has already been prepared.
    uid = str(previous[0]) if previous else str(uuid.uuid4())
    if previous:
        conn.execute("UPDATE hot_snapshots SET content_hash=?, user_text=?, agent_text=?, current_text=?, source_claim_ids=?, created_at=?, last_checked_at=? WHERE snapshot_uid=?", (digest, texts["USER.md"], texts["AGENT.md"], texts["CURRENT.md"], json.dumps(ids, ensure_ascii=False), utc_now(), utc_now(), uid))
    else:
        conn.execute("INSERT INTO hot_snapshots(snapshot_uid, workspace_id, profile_id, subject_id, agent_id, session_id, content_hash, user_text, agent_text, current_text, source_claim_ids) VALUES(?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?)", (uid, workspace_id, profile_id, subject_id, agent_id, digest, texts["USER.md"], texts["AGENT.md"], texts["CURRENT.md"], json.dumps(ids, ensure_ascii=False)))
    conn.commit(); conn.close()
    directory = hot_scope_dir(root, workspace_id=workspace_id, profile_id=profile_id, subject_id=subject_id, agent_id=agent_id)
    for name, text in texts.items():
        path = directory / name
        if force or not path.exists() or path.read_text(encoding="utf-8") != text: _atomic_write(path, text)
    snapshot = {"schema_version":2,"snapshot_uid":uid,"workspace_id":workspace_id,"profile_id":profile_id,"subject_id":subject_id,"agent_id":agent_id,"content_hash":digest,"source_claim_ids":ids,"budgets":{"USER.md":int(get("hot_memory.user_max_chars")),"AGENT.md":int(get("hot_memory.agent_max_chars")),"CURRENT.md":int(get("hot_memory.current_max_chars"))}}
    _atomic_write(directory / "snapshot.json", json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    return {**snapshot,"changed":True,"scope":str(directory)}


def freeze_hot_snapshot(root: Path, *, internal_session_id: str, subject_id: str, profile_id: str = "default", workspace_id: str = "default", agent_id: str = "", policy: str = "frozen") -> dict[str, object]:
    conn = open_db(root)
    row = conn.execute("SELECT s.hot_snapshot_uid, s.hot_snapshot_hash FROM sessions s JOIN hot_snapshots h ON h.snapshot_uid=s.hot_snapshot_uid WHERE s.session_id=? AND s.subject_id=? AND s.workspace_id=? AND s.profile_id=? AND h.agent_id=?", (internal_session_id, subject_id, workspace_id, profile_id, agent_id)).fetchone()
    if policy in {"frozen", "manual"} and row and row[0]:
        uid = str(row[0]); conn.close(); return load_hot_memory(root, subject_id=subject_id, profile_id=profile_id, workspace_id=workspace_id, snapshot_uid=uid)
    conn.close()
    latest = build_hot_memory(root, subject_id=subject_id, profile_id=profile_id, workspace_id=workspace_id, agent_id=agent_id)
    uid = str(latest["snapshot_uid"])
    conn = open_db(root)
    source = conn.execute("SELECT user_text, agent_text, current_text, source_claim_ids FROM hot_snapshots WHERE snapshot_uid=?", (uid,)).fetchone()
    session_uid = str(uuid.uuid4())
    conn.execute("INSERT OR REPLACE INTO hot_snapshots(snapshot_uid, workspace_id, profile_id, subject_id, agent_id, session_id, content_hash, user_text, agent_text, current_text, source_claim_ids) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (session_uid, workspace_id, profile_id, subject_id, agent_id, internal_session_id, latest["content_hash"], *source))
    conn.execute("UPDATE sessions SET hot_snapshot_uid=?, hot_snapshot_hash=?, hot_snapshot_created_at=? WHERE session_id=?", (session_uid, latest["content_hash"], utc_now(), internal_session_id)); conn.commit(); conn.close()
    return load_hot_memory(root, subject_id=subject_id, profile_id=profile_id, workspace_id=workspace_id, snapshot_uid=session_uid)


def load_hot_memory(root: Path, *, subject_id: str, profile_id: str, workspace_id: str, snapshot_uid: str = "") -> dict[str, object]:
    conn = open_db(root)
    if snapshot_uid: row = conn.execute("SELECT snapshot_uid, content_hash, user_text, agent_text, current_text, source_claim_ids FROM hot_snapshots WHERE snapshot_uid=? AND subject_id=? AND profile_id=? AND workspace_id=?", (snapshot_uid, subject_id, profile_id, workspace_id)).fetchone()
    else: row = conn.execute("SELECT snapshot_uid, content_hash, user_text, agent_text, current_text, source_claim_ids FROM hot_snapshots WHERE subject_id=? AND profile_id=? AND workspace_id=? AND session_id='' ORDER BY created_at DESC LIMIT 1", (subject_id, profile_id, workspace_id)).fetchone()
    conn.close()
    if not row: return {"snapshot_uid":"","content_hash":"","content":"","source_claim_ids":[]}
    content = "\n\n".join(part.rstrip() for part in row[2:5] if str(part).strip()).strip() + "\n"
    return {"snapshot_uid":str(row[0]),"content_hash":str(row[1]),"content":content,"source_claim_ids":json.loads(str(row[5] or "[]"))}


def garbage_collect_snapshots(root: Path, *, compact_after_days: int = 90, remove_after_days: int = 365) -> dict[str, int]:
    """Retain audit hashes/source IDs while bounding completed-session text."""
    from datetime import datetime, timedelta, timezone

    conn = open_db(root)
    compact_cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, compact_after_days))).isoformat()
    remove_cutoff = (datetime.now(timezone.utc) - timedelta(days=max(compact_after_days + 1, remove_after_days))).isoformat()
    compacted = conn.execute(
        """UPDATE hot_snapshots SET user_text='', agent_text='', current_text=''
           WHERE session_id!='' AND created_at<? AND (user_text!='' OR agent_text!='' OR current_text!='')""",
        (compact_cutoff,),
    ).rowcount
    removed = conn.execute(
        """DELETE FROM hot_snapshots WHERE session_id!='' AND created_at<?
           AND snapshot_uid NOT IN (SELECT hot_snapshot_uid FROM sessions WHERE status='active' AND hot_snapshot_uid IS NOT NULL)""",
        (remove_cutoff,),
    ).rowcount
    conn.commit(); conn.close()
    return {"compacted": int(compacted), "removed": int(removed)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile scope-isolated hot memory from verified sourced claims.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP); parser.add_argument("--subject-id", required=True); parser.add_argument("--profile-id", default="default"); parser.add_argument("--workspace-id", default="default"); parser.add_argument("--agent-id", default=""); parser.add_argument("--force", action="store_true")
    args = parser.parse_args(); emit(build_hot_memory(store_root(args.store), subject_id=args.subject_id, profile_id=args.profile_id, workspace_id=args.workspace_id, agent_id=args.agent_id, force=args.force))


if __name__ == "__main__": main()
