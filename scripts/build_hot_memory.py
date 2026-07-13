#!/usr/bin/env python3
"""Compile bounded, read-only hot-memory projections from eligible claims."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, utc_now, store_root
from config import get
from security_scan import sanitize_for_context


TYPE_PRIORITY = {"profile": 1.0, "state": 0.9, "goal": 0.85, "relationship": 0.75, "domain": 0.65, "event": 0.35}


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def hot_memory_score(claim: dict[str, object]) -> float:
    text = str(claim["content"])
    score = _float(claim.get("importance")) * 0.25
    score += _float(claim.get("confidence")) * 0.20
    score += max(-1.0, min(1.0, _float(claim.get("confirmed_utility")))) * 0.15
    score += _float(claim.get("durability"), 0.5) * 0.15
    score += TYPE_PRIORITY.get(str(claim.get("memory_kind")), 0.2) * 0.15
    valid_from = str(claim.get("valid_from") or "")
    score += 0.10 if not valid_from or valid_from <= utc_now() else 0.0
    score -= min(0.18, len(text) / 12000)
    score -= _float(claim.get("uncertainty")) * 0.12
    if str(claim.get("sensitivity")) == "sensitive":
        score -= 0.35
    return round(score, 6)


def _read_claims(root: Path, subject_id: str) -> list[dict[str, object]]:
    conn = open_db(root)
    rows = conn.execute(
        """
        SELECT c.id, c.memory_kind, c.domain, c.topic, c.title, c.content, c.status, c.verification_state,
               c.confidence, c.importance, c.sensitivity, c.valid_from, c.valid_to, c.durability,
               c.confirmed_utility, c.security_state,
               (SELECT COUNT(*) FROM claim_sources cs WHERE cs.claim_id=c.id) AS sources
        FROM claims c
        WHERE c.subject_id=? AND c.status='active' AND c.prompt_eligible=1
          AND c.security_state IN ('clean', 'reviewed_safe')
          AND c.verification_state NOT IN ('unverified', 'disputed', 'invalid')
          AND (c.valid_from IS NULL OR c.valid_from='' OR c.valid_from<=?)
          AND (c.valid_to IS NULL OR c.valid_to='' OR c.valid_to>?)
          AND (c.replaced_by IS NULL OR c.replaced_by='')
        """,
        (subject_id, utc_now(), utc_now()),
    ).fetchall()
    conn.close()
    keys = ["id", "memory_kind", "domain", "topic", "title", "content", "status", "verification_state", "confidence", "importance", "sensitivity", "valid_from", "valid_to", "durability", "confirmed_utility", "security_state", "sources"]
    return [dict(zip(keys, row)) for row in rows if int(row[-1] or 0) > 0]


def _render(title: str, items: list[dict[str, object]], budget: int) -> tuple[str, list[str]]:
    lines = [f"# {title}", "", "Generated from eligible claims; do not edit this projection manually."]
    ids: list[str] = []
    for item in items:
        text = sanitize_for_context(str(item["content"]).strip())
        line = f"- {text}  <!-- claim:{item['id']} -->"
        candidate = "\n".join(lines + [line]) + "\n"
        if len(candidate) > budget:
            continue
        lines.append(line)
        ids.append(str(item["id"]))
    if not ids:
        lines.append("- No eligible memory is currently available.")
    return "\n".join(lines).rstrip() + "\n", ids


def build_hot_memory(root: Path, *, subject_id: str, profile_id: str = "default", force: bool = False) -> dict[str, object]:
    claims = _read_claims(root, subject_id)
    for claim in claims:
        claim["score"] = hot_memory_score(claim)
    claims.sort(key=lambda item: (float(item["score"]), float(item["importance"])), reverse=True)
    quotas = {"profile": 5, "state": 3, "goal": 3, "relationship": 2, "domain": 3}
    chosen: list[dict[str, object]] = []
    counts: dict[str, int] = {}
    for claim in claims:
        kind = str(claim["memory_kind"])
        if counts.get(kind, 0) >= quotas.get(kind, 1):
            continue
        counts[kind] = counts.get(kind, 0) + 1
        chosen.append(claim)
    user = [item for item in chosen if str(item["memory_kind"]) in {"profile", "relationship"}]
    current = [item for item in chosen if str(item["memory_kind"]) in {"state", "goal", "event"}]
    agent = [item for item in chosen if str(item["memory_kind"]) == "domain"]
    hot_dir = root / "hot"
    hot_dir.mkdir(parents=True, exist_ok=True)
    rendered = {
        "USER.md": _render("Core User Memory", user, int(get("hot_memory.user_max_chars"))),
        "AGENT.md": _render("Agent Operating Principles", agent, int(get("hot_memory.agent_max_chars"))),
        "CURRENT.md": _render("Current Priorities", current, int(get("hot_memory.current_max_chars"))),
    }
    hashes: dict[str, str] = {}
    source_claims: dict[str, list[str]] = {}
    changed = False
    for filename, (content, ids) in rendered.items():
        path = hot_dir / filename
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        hashes[filename] = digest
        source_claims[filename] = ids
        if force or not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8", newline="\n")
            changed = True
    snapshot = {"schema_version": 1, "subject_id": subject_id, "profile_id": profile_id, "generated_at": utc_now(), "hashes": hashes, "source_claim_ids": source_claims, "budgets": {"USER.md": int(get("hot_memory.user_max_chars")), "AGENT.md": int(get("hot_memory.agent_max_chars")), "CURRENT.md": int(get("hot_memory.current_max_chars"))}}
    snapshot_text = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    snapshot_path = hot_dir / "snapshot.json"
    if force or not snapshot_path.exists() or snapshot_path.read_text(encoding="utf-8") != snapshot_text:
        snapshot_path.write_text(snapshot_text, encoding="utf-8", newline="\n")
        changed = True
    snapshot["changed"] = changed
    return snapshot


def load_hot_memory(root: Path) -> tuple[str, str]:
    hot_dir = root / "hot"
    parts: list[str] = []
    hashes: list[str] = []
    for name in ("USER.md", "AGENT.md", "CURRENT.md"):
        path = hot_dir / name
        if path.exists():
            content = path.read_text(encoding="utf-8")
            parts.append(content.rstrip())
            hashes.append(hashlib.sha256(content.encode("utf-8")).hexdigest())
    return "\n\n".join(parts).strip() + ("\n" if parts else ""), hashlib.sha256("".join(hashes).encode("utf-8")).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile bounded hot memory from active verified claims.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--profile-id", default="default")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    emit(build_hot_memory(store_root(args.store), subject_id=args.subject_id, profile_id=args.profile_id, force=args.force))


if __name__ == "__main__":
    main()
