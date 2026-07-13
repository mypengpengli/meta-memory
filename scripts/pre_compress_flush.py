"""Preserve uncompressed conversation evidence before host context compaction."""
from __future__ import annotations

from build_session_card import build_cards
from consolidate_memories import build_plan
from extract_memory_units import extract_units
from ingest_raw_event import insert_raw_event


def on_pre_compress(*, root, subject_id: str, session_id: str, messages: list[dict]) -> dict[str, object]:
    ids: list[int] = []
    for message in messages:
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        result = insert_raw_event(root, subject_id=subject_id, subject_name=str(message.get("subject_name") or "Unknown"), session_id=session_id, source_type=f"conversation-{message.get('role', 'user')}", content=content, allow_duplicate=False)
        if result.get("inserted"):
            ids.append(int(result["raw_event_id"]))
    build_cards(root, subject_id=subject_id, session_id=session_id, force=True)
    units = extract_units(root, subject_id=subject_id, limit=100)
    plan = build_plan(root, subject_id, policy="conservative", limit=100)
    hints = [str(item.get("content", ""))[:240] for item in plan.get("actions", []) if str(item.get("content", ""))][:8]
    return {"flushed_event_ids": ids, "memory_units_created": units["created"], "compression_hints": hints, "shadow_plan": plan}
