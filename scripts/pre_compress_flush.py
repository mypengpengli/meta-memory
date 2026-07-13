"""Preserve uncompressed conversation evidence before host context compaction."""
from __future__ import annotations

from background_review import enqueue_review
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
    review = enqueue_review(
        root, subject_id=subject_id, session_id=session_id,
        event_start_id=min(ids) if ids else 0, event_end_id=max(ids) if ids else 0,
        trigger_type="pre_compress",
    ) if ids else {"status": "not_scheduled", "reason": "no_new_events"}
    return {"flushed_event_ids": ids, "memory_units_created": [], "compression_hints": [], "review": review}
