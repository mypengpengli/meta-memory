#!/usr/bin/env python3
"""Validate memory plans before they can affect claims or Markdown files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, sha256_text, store_root
from config import get
from security_scan import scan_memory_content, security_state


ACTIONS = {"CREATE", "CORROBORATE", "REFINE", "CORRECT", "SUPERSEDE", "IGNORE"}
LONG_TERM = {"profile", "state", "event", "relationship", "goal", "domain"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a JSON memory consolidation plan.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--plan-file", required=True)
    return parser.parse_args()


def load_plan(path: str) -> dict[str, object]:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or not isinstance(value.get("actions"), list):
        raise ValueError("Memory plan must be an object with an actions array.")
    return value


def source_ids(action: dict[str, object]) -> list[int]:
    values = action.get("source_event_ids", [])
    if not isinstance(values, list):
        return []
    result: list[int] = []
    for value in values:
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            pass
    return list(dict.fromkeys(result))


def _semantic_key(action: dict[str, object], subject_id: str) -> str:
    visibility = str(action.get("visibility_scope") or "global")
    parts: list[object] = [
        str(action.get("profile_id") or "default"),
        str(action.get("workspace_id") or "global"),
        subject_id,
        visibility,
    ]
    if visibility == "agent":
        parts.append(str(action.get("owner_agent_id") or ""))
    parts.extend(
        [
            str(action.get("predicate") or "states"),
            str(action.get("subject_text") or "user"),
            str(action.get("object_text") or str(action.get("content") or "")[:240]),
            action.get("qualifiers") or {},
        ]
    )
    return sha256_text(
        json.dumps(
            parts,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def validate_plan(root, plan: dict[str, object]) -> dict[str, object]:
    conn = open_db(root)
    errors: list[dict[str, object]] = []
    warnings: list[dict[str, object]] = []
    plan_subject = str(plan.get("subject_id", ""))
    seen_ids: set[str] = set()
    for index, raw in enumerate(plan.get("actions", [])):
        if not isinstance(raw, dict):
            errors.append({"index": index, "code": "invalid_action", "message": "Action must be an object."})
            continue
        action = raw
        action_id = str(action.get("plan_id", ""))
        action_name = str(action.get("action", "")).upper()
        subject_id = str(action.get("subject_id", plan_subject))
        if not action_id or action_id in seen_ids:
            errors.append({"index": index, "code": "plan_id", "message": "Every action needs a unique plan_id."})
        seen_ids.add(action_id)
        if action_name not in ACTIONS:
            errors.append({"index": index, "code": "action", "message": f"Unsupported action: {action_name}."})
        if not subject_id:
            errors.append({"index": index, "code": "subject", "message": "A subject_id is required."})
        ids = source_ids(action)
        source_types: set[str] = set()
        if action_name != "IGNORE" and not ids:
            errors.append({"index": index, "code": "sources", "message": "Non-IGNORE actions require source_event_ids."})
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            found = conn.execute(f"SELECT id, subject_id, source_type FROM raw_events WHERE id IN ({placeholders})", tuple(ids)).fetchall()
            if len(found) != len(ids):
                errors.append({"index": index, "code": "sources_missing", "message": "One or more source events do not exist."})
            if any(str(row[1]) != subject_id for row in found):
                errors.append({"index": index, "code": "cross_subject_source", "message": "Sources must belong to the action subject."})
            source_types = {str(row[2] or "").casefold() for row in found}
            if str(action.get("memory_kind", "")) == "profile" and all(str(row[2]) == "conversation-assistant" for row in found):
                errors.append({"index": index, "code": "assistant_profile", "message": "Assistant-only evidence cannot modify a profile."})
        tool_backed = str(action.get("source_type") or "").casefold() in {"agent-observation", "tool-result"} or bool(source_types & {"agent-observation", "tool-result"})
        if tool_backed:
            if str(action.get("memory_kind") or "").casefold() == "profile":
                errors.append({"index": index, "code": "agent_profile", "message": "Agent or tool evidence cannot create or modify a user profile."})
            if action_name in {"REFINE", "CORRECT", "SUPERSEDE"}:
                errors.append({"index": index, "code": "agent_mutation", "message": "Agent or tool evidence cannot rewrite an existing claim."})
            if action_name == "CREATE":
                existing = conn.execute(
                    "SELECT memory_kind FROM claims WHERE profile_id=? AND workspace_id=? AND subject_id=? AND visibility_scope=? AND COALESCE(owner_agent_id,'')=? AND semantic_key=? AND status='active'",
                    (
                        str(action.get("profile_id") or "default"),
                        str(action.get("workspace_id") or "global"),
                        subject_id,
                        str(action.get("visibility_scope") or "global"),
                        str(action.get("owner_agent_id") or "") if str(action.get("visibility_scope") or "global") == "agent" else "",
                        _semantic_key(action, subject_id),
                    ),
                ).fetchone()
                if existing and str(existing[0]) == "profile":
                    errors.append({"index": index, "code": "agent_profile_collision", "message": "Agent or tool evidence cannot corroborate an existing user profile."})
        requested_path = str(action.get("target_path", ""))
        if requested_path:
            candidate = Path(requested_path).expanduser().resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                errors.append({"index": index, "code": "path_traversal", "message": "target_path must stay inside memory-data."})
        target_id = str(action.get("target_claim_id", ""))
        if action_name in {"CORROBORATE", "REFINE", "CORRECT", "SUPERSEDE"}:
            if not target_id:
                errors.append({"index": index, "code": "target", "message": f"{action_name} requires target_claim_id."})
            else:
                row = conn.execute("SELECT subject_id,memory_kind,profile_id,workspace_id,visibility_scope,owner_agent_id FROM claims WHERE id=?", (target_id,)).fetchone()
                if row is None:
                    errors.append({"index": index, "code": "target_missing", "message": "Target claim does not exist."})
                elif str(row[0]) != subject_id:
                    errors.append({"index": index, "code": "cross_subject_target", "message": "Target claim belongs to another subject."})
                elif tool_backed and str(row[1]) == "profile":
                    errors.append({"index": index, "code": "agent_profile_target", "message": "Agent or tool evidence cannot alter or corroborate a user profile."})
                else:
                    target_scope = (str(row[2]), str(row[3]), str(row[4]), str(row[5] or ""))
                    action_scope = (
                        str(action.get("profile_id") or row[2]),
                        str(action.get("workspace_id") or row[3]),
                        str(action.get("visibility_scope") or row[4]),
                        str(action.get("owner_agent_id") or row[5] or ""),
                    )
                    if target_scope != action_scope:
                        errors.append({"index": index, "code": "target_scope", "message": "Target claim scope must match the action scope."})
                    if str(row[4]) == "agent" and str(row[5] or "") != str(action.get("origin_agent_id") or ""):
                        errors.append({"index": index, "code": "agent_private_target", "message": "Only the owning Agent may modify an agent-private claim."})
        if action_name == "REFINE" and bool(action.get("auto_promote")) and not bool(action.get("refine_safe")):
            errors.append({"index": index, "code": "unsafe_auto_refine", "message": "Automatic REFINE requires deterministic additive refinement evidence."})
        if action_name in {"CORRECT", "SUPERSEDE"} and len(ids) < 2 and bool(get("consolidation.require_two_sources_for_correct")):
            errors.append({"index": index, "code": "weak_temporal_change", "message": "CORRECT and SUPERSEDE require at least two evidence events."})
        kind = str(action.get("memory_kind", "candidate"))
        confidence = float(action.get("confidence", 0.0) or 0.0)
        sensitive = str(action.get("sensitivity", "normal")) == "sensitive"
        if kind == "profile" and confidence < float(get("consolidation.profile_confidence_threshold")):
            errors.append({"index": index, "code": "profile_threshold", "message": "Profile changes require high confidence."})
        if sensitive and confidence < float(get("consolidation.sensitive_confidence_threshold")):
            errors.append({"index": index, "code": "sensitivity_threshold", "message": "Sensitive facts require the stricter confidence threshold."})
        if kind in LONG_TERM and str(action.get("verification_state", "unverified")) == "verified" and confidence < 0.8:
            errors.append({"index": index, "code": "unverified_promotion", "message": "Low-confidence candidate cannot become verified long-term memory."})
        if action_name == "CREATE" and not str(action.get("content", "")).strip():
            errors.append({"index": index, "code": "content", "message": "CREATE requires claim content."})
        if action_name in {"CORRECT", "SUPERSEDE"} and not str(action.get("content", "")).strip():
            errors.append({"index": index, "code": "replacement_content", "message": f"{action_name} requires replacement content."})
        if action_name in {"CORRECT", "SUPERSEDE"}:
            warnings.append({"index": index, "code": "review_recommended", "message": f"{action_name} is a high-risk historical change and normally enters review."})
        content = str(action.get("content", ""))
        findings = scan_memory_content(content, source_type="memory_plan")
        state, _ = security_state(findings)
        if state == "blocked":
            errors.append({"index": index, "code": "blocked_memory_content", "message": "Unsafe or instruction-like memory content cannot be applied."})
        valid_from = str(action.get("valid_from") or action.get("start_at") or "")
        valid_to = str(action.get("valid_to") or action.get("end_at") or "")
        if valid_from and valid_to and valid_from > valid_to:
            errors.append({"index": index, "code": "invalid_time_range", "message": "valid_from must not be later than valid_to."})
    conn.close()
    return {"status": "ok", "valid": not errors, "errors": errors, "warnings": warnings, "action_count": len(plan.get("actions", []))}


def main() -> None:
    args = parse_args()
    emit(validate_plan(store_root(args.store), load_plan(args.plan_file)))


if __name__ == "__main__":
    main()
