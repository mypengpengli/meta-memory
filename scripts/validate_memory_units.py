"""Validate atomic extracted units before consolidation can reason over them."""
from __future__ import annotations

import json
from datetime import datetime

from _common import open_db, sha256_text
from security_scan import findings_json, scan_memory_content, security_state


def _number(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def validate_unit(root, unit: dict[str, object]) -> dict[str, object]:
    errors: list[str] = []
    text = str(unit.get("claim_text") or unit.get("content") or "").strip()
    predicate = str(unit.get("predicate") or "").strip()
    sources = [int(value) for value in unit.get("source_event_ids", []) if str(value).isdigit()]
    if not text:
        errors.append("claim_text_required")
    if not predicate:
        errors.append("single_predicate_required")
    if not sources:
        errors.append("source_event_ids_required")
    for key in ("confidence", "uncertainty", "importance", "durability"):
        value = _number(unit.get(key), 0.5)
        if not 0.0 <= value <= 1.0:
            errors.append(f"{key}_out_of_range")
    valid_from, valid_to = str(unit.get("valid_from") or ""), str(unit.get("valid_to") or "")
    if valid_from and valid_to and valid_from > valid_to:
        errors.append("invalid_time_range")
    conn = open_db(root)
    if sources:
        placeholders = ", ".join("?" for _ in sources)
        events = conn.execute(f"SELECT id, subject_id, source_type, content FROM raw_events WHERE id IN ({placeholders})", sources).fetchall()
        if len(events) != len(sources):
            errors.append("source_event_missing")
        subject_id = str(unit.get("subject_id") or "")
        if subject_id and any(str(row[1]) != subject_id for row in events):
            errors.append("cross_subject_source")
        if str(unit.get("memory_kind") or unit.get("unit_kind") or "") == "profile" and events and all("assistant" in str(row[2]) for row in events):
            errors.append("assistant_only_profile")
    subject_id = str(unit.get("subject_id") or "")
    duplicate = conn.execute("SELECT id FROM memory_units WHERE subject_id=? AND raw_event_id IN ({}) AND content_hash=? LIMIT 1".format(", ".join("?" for _ in sources)), (subject_id, *sources, sha256_text(text))).fetchone() if subject_id and text and sources else None
    conn.close()
    if duplicate and not bool(unit.get("allow_existing")):
        errors.append("duplicate_unit")
    findings = scan_memory_content(text, source_type="memory_unit")
    state, eligible = security_state(findings)
    if state == "blocked":
        errors.append("blocked_memory_content")
    return {"valid": not errors, "errors": errors, "security_state": state, "security_findings": findings_json(findings), "prompt_eligible": eligible}
