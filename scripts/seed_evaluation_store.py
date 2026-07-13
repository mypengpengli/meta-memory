#!/usr/bin/env python3
"""Seed a small deterministic store used only by the checked-in CI evaluators."""
from __future__ import annotations

import argparse
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, store_root
from apply_memory_plan import apply_plan
from ingest_raw_event import insert_raw_event
from projection_outbox import process_projection_outbox


def main() -> None:
    parser = argparse.ArgumentParser(description="Create deterministic evaluation fixtures in a local store.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    args = parser.parse_args()
    root = store_root(args.store)
    subject = "evaluation-user"
    database_event = insert_raw_event(root, subject_id=subject, subject_name="Evaluation User", source_type="fixture", content="The project uses PostgreSQL.", allow_duplicate=True)
    style_event = insert_raw_event(root, subject_id=subject, subject_name="Evaluation User", source_type="fixture", content="I prefer concise answers with concrete steps.", allow_duplicate=True)
    old_event = insert_raw_event(root, subject_id=subject, subject_name="Evaluation User", source_type="fixture", content="The project previously used SQLite.", allow_duplicate=True)
    ids = [int(item["raw_event_id"]) for item in (database_event, style_event, old_event)]
    actions = [
        {"plan_id": "evaluation-current-db", "action": "CREATE", "subject_id": subject, "source_event_ids": [ids[0]], "memory_kind": "state", "domain": "work", "topic": "database", "title": "Current Database", "content": "The project uses PostgreSQL.", "predicate": "uses_database", "subject_text": "project", "object_text": "PostgreSQL", "confidence": .95, "importance": .9, "durability": .9, "sensitivity": "normal", "verification_state": "verified", "valid_from": "2025-01-01T00:00:00+00:00"},
        {"plan_id": "evaluation-style", "action": "CREATE", "subject_id": subject, "source_event_ids": [ids[1]], "memory_kind": "profile", "domain": "general", "topic": "response-style", "title": "Response Style", "content": "I prefer concise answers with concrete steps.", "predicate": "prefers", "subject_text": "user", "object_text": "concise concrete steps", "confidence": .95, "importance": .9, "durability": .9, "sensitivity": "normal", "verification_state": "verified", "valid_from": "2024-01-01T00:00:00+00:00"},
        {"plan_id": "evaluation-old-db", "action": "CREATE", "subject_id": subject, "source_event_ids": [ids[2]], "memory_kind": "state", "domain": "work", "topic": "database", "title": "Historical Database", "content": "The project previously used SQLite.", "predicate": "uses_database", "subject_text": "project", "object_text": "SQLite", "confidence": .9, "importance": .7, "durability": .8, "sensitivity": "normal", "verification_state": "verified", "valid_from": "2024-01-01T00:00:00+00:00", "valid_to": "2025-01-01T00:00:00+00:00"},
    ]
    applied = apply_plan(root, {"schema_version": 3, "subject_id": subject, "policy": "balanced", "actions": actions}, review_approved=True)
    projections = process_projection_outbox(root)
    emit({"status": "ok", "events": ids, "applied": applied["results"], "projections": projections})


if __name__ == "__main__":
    main()
