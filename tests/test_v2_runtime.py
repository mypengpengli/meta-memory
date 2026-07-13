from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _common import open_db
from apply_memory_plan import apply_plan
from build_session_card import build_cards
from consolidate_memories import build_plan
from extract_memory_units import extract_units
from ingest_raw_event import insert_raw_event
from node_search import search_nodes
from validate_memory_plan import validate_plan


class MetaMemoryV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_migrations_session_units_and_consolidation_are_idempotent(self) -> None:
        conn = open_db(self.root)
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        conn.close()
        self.assertEqual(versions, ["001", "002", "003", "004", "005", "006", "007", "008"])
        first = insert_raw_event(self.root, subject_id="person:test", subject_name="Test", session_id="s1", source_type="conversation-user", content="I prefer troubleshooting answers with causes and executable steps.")
        self.assertTrue(first["inserted"])
        cards = build_cards(self.root, subject_id="person:test", force=True)
        self.assertEqual(cards["cards"][0]["event_count"], 1)
        units = extract_units(self.root, subject_id="person:test")
        self.assertEqual(len(units["created"]), 1)
        plan = build_plan(self.root, "person:test", policy="conservative")
        self.assertEqual(plan["actions"][0]["action"], "CREATE")
        self.assertTrue(validate_plan(self.root, plan)["valid"])
        applied = apply_plan(self.root, plan, skip_index=True)
        self.assertEqual(applied["results"][0]["status"], "applied")
        claim_id = applied["results"][0]["claim_id"]
        second = insert_raw_event(self.root, subject_id="person:test", subject_name="Test", session_id="s2", source_type="conversation-user", content="I prefer troubleshooting answers with causes and executable steps.")
        self.assertTrue(second["inserted"])
        build_cards(self.root, subject_id="person:test", force=True)
        extract_units(self.root, subject_id="person:test")
        corroboration = build_plan(self.root, "person:test", policy="conservative")
        self.assertEqual(corroboration["actions"][0]["action"], "CORROBORATE")
        self.assertEqual(corroboration["actions"][0]["target_claim_id"], claim_id)
        self.assertEqual(apply_plan(self.root, corroboration, skip_index=True)["results"][0]["status"], "applied")

    def test_legacy_minimal_schema_is_migrated_without_data_loss(self) -> None:
        db = self.root / "db" / "memory_index.sqlite"
        db.parent.mkdir(parents=True)
        legacy = sqlite3.connect(db)
        legacy.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL)")
        legacy.execute("CREATE TABLE scores (path TEXT PRIMARY KEY)")
        legacy.execute("CREATE TABLE raw_events (id INTEGER PRIMARY KEY)")
        legacy.execute("CREATE TABLE maintenance_cursor (subject_id TEXT PRIMARY KEY)")
        legacy.execute("CREATE TABLE memory_sources (id INTEGER PRIMARY KEY)")
        legacy.commit()
        legacy.close()
        conn = open_db(self.root)
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        columns = [row[1] for row in conn.execute("PRAGMA table_info(raw_events)")]
        conn.close()
        self.assertEqual(versions, ["001", "002", "003", "004", "005", "006", "007", "008"])
        self.assertIn("content", columns)
        self.assertIn("session_card_id", columns)

    def test_scope_and_temporal_corrections_are_guarded(self) -> None:
        event_one = insert_raw_event(self.root, subject_id="person:test", subject_name="Test", source_type="explicit-memory", content="The project uses SQLite.")
        event_two = insert_raw_event(self.root, subject_id="person:test", subject_name="Test", source_type="explicit-memory", content="The project now uses PostgreSQL.")
        create = {"subject_id": "person:test", "policy": "balanced", "actions": [{"plan_id": "create-one", "action": "CREATE", "subject_id": "person:test", "source_event_ids": [event_one["raw_event_id"]], "memory_kind": "domain", "topic": "storage", "title": "Storage", "content": "The project uses SQLite.", "confidence": 0.95, "importance": 0.8, "sensitivity": "normal", "verification_state": "verified"}]}
        created = apply_plan(self.root, create, skip_index=True)
        claim_id = created["results"][0]["claim_id"]
        correction = {"subject_id": "person:test", "policy": "balanced", "actions": [{"plan_id": "replace-one", "action": "SUPERSEDE", "subject_id": "person:test", "target_claim_id": claim_id, "source_event_ids": [event_one["raw_event_id"], event_two["raw_event_id"]], "memory_kind": "domain", "topic": "storage", "title": "Storage", "content": "The project now uses PostgreSQL.", "confidence": 0.95, "importance": 0.8, "sensitivity": "normal", "verification_state": "verified"}]}
        self.assertTrue(validate_plan(self.root, correction)["valid"])
        queued = apply_plan(self.root, correction, skip_index=True)
        self.assertEqual(queued["results"][0]["status"], "review")
        conn = open_db(self.root)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0], 1)
        conn.close()

    def test_node_search_never_crosses_subject_scope(self) -> None:
        event = insert_raw_event(self.root, subject_id="person:other", subject_name="Other", source_type="explicit-memory", content="Other person prefers concise answers.")
        plan = {"subject_id": "person:other", "policy": "balanced", "actions": [{"plan_id": "other-create", "action": "CREATE", "subject_id": "person:other", "source_event_ids": [event["raw_event_id"]], "memory_kind": "profile", "topic": "style", "title": "Other style", "content": "Other person prefers concise answers.", "confidence": 0.95, "importance": 0.8, "sensitivity": "normal", "verification_state": "verified"}]}
        apply_plan(self.root, plan, skip_index=True)
        result = search_nodes(self.root, "person:test", "concise answers")
        self.assertEqual(result["returned"], 0)


if __name__ == "__main__":
    unittest.main()
