from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
import sys
sys.path.insert(0, str(SCRIPTS))

from _common import open_db
from apply_memory_plan import apply_plan
from background_review import enqueue_review, run_pending
from build_hot_memory import build_hot_memory
from build_session_card import build_cards
from consolidate_memories import build_plan
from extract_memory_units import extract_units
from ingest_raw_event import insert_raw_event
from procedural_learning import create_learning, retrieve_procedures
from projection_outbox import _claim_batch, enqueue_projection, process_projection_outbox
from retrieve_memories import retrieve
from session_archive import ensure_session
from memory_api import authorize, identity, load_principals


class MetaMemoryV23SharedRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.subject = "person:shared"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def event(self, text: str, *, workspace: str, agent: str = "", visibility: str = "workspace", session: str = "s") -> int:
        return int(insert_raw_event(
            self.root, subject_id=self.subject, subject_name="Shared", session_id=session,
            source_type="conversation-user", content=text, profile_id="profile-a",
            workspace_id=workspace, origin_agent_id=agent, visibility_scope=visibility,
        )["raw_event_id"])

    def claim(self, text: str, event_id: int, *, workspace: str, agent: str = "", visibility: str = "workspace") -> str:
        action = {
            "plan_id": str(uuid.uuid4()), "action": "CREATE", "subject_id": self.subject,
            "subject_name": "Shared", "source_event_ids": [event_id], "memory_kind": "state",
            "domain": "work", "topic": "scope", "title": text[:30], "content": text,
            "predicate": "states", "subject_text": "project", "object_text": text,
            "confidence": .95, "importance": .9, "durability": .8, "sensitivity": "normal",
            "verification_state": "verified", "profile_id": "profile-a", "workspace_id": workspace,
            "origin_agent_id": agent, "visibility_scope": visibility,
            "owner_agent_id": agent if visibility == "agent" else "",
        }
        result = apply_plan(self.root, {"schema_version": 3, "subject_id": self.subject, "policy": "balanced", "actions": [action]}, skip_index=True)
        self.assertEqual(result["results"][0]["status"], "applied")
        return str(result["results"][0]["claim_id"])

    def retrieval_args(self, *, workspace: str, agent: str, query: str) -> argparse.Namespace:
        return argparse.Namespace(
            store=str(self.root), query=query, query_file=None, top_k=12, candidate_pool=30,
            expand_hops=0, session_id="", workspace_id=workspace, profile_id="profile-a",
            agent_id=agent, active_subject_id=[], valid_at=None, no_chunks=False,
            include_embeddings=False, embedding_model="external", rrf_k=60,
            subject_id=self.subject, subject_name=None, domain=[], memory_kind=[],
            include_candidates=False, no_basics=True,
        )

    def test_visibility_is_enforced_for_retrieval_and_hot_memory(self) -> None:
        global_id = self.event("global-token", workspace="global", visibility="global")
        work_id = self.event("workspace-token", workspace="project-a", agent="agent-a")
        agent_id = self.event("agent-token", workspace="project-a", agent="agent-a", visibility="agent")
        self.claim("global-token", global_id, workspace="global", visibility="global")
        self.claim("workspace-token", work_id, workspace="project-a", agent="agent-a")
        self.claim("agent-token", agent_id, workspace="project-a", agent="agent-a", visibility="agent")
        process_projection_outbox(self.root, limit=20)

        other = retrieve(self.retrieval_args(workspace="project-b", agent="agent-b", query="token"))
        local = retrieve(self.retrieval_args(workspace="project-a", agent="agent-a", query="token"))
        self.assertEqual({item["title"] for item in other["selected"]}, {"global-token"})
        self.assertEqual({item["title"] for item in local["selected"]}, {"global-token", "workspace-token", "agent-token"})

        owner_hot = build_hot_memory(self.root, subject_id=self.subject, profile_id="profile-a", workspace_id="project-a", agent_id="agent-a")
        other_hot = build_hot_memory(self.root, subject_id=self.subject, profile_id="profile-a", workspace_id="project-a", agent_id="agent-b")
        self.assertNotEqual(owner_hot["scope"], other_hot["scope"])
        self.assertIn("agent-token", (Path(owner_hot["scope"]) / "CURRENT.md").read_text(encoding="utf-8"))
        self.assertNotIn("agent-token", (Path(other_hot["scope"]) / "CURRENT.md").read_text(encoding="utf-8"))

    def test_ingest_is_idempotent_and_shared_mode_rejects_implicit_sessions(self) -> None:
        first = insert_raw_event(self.root, subject_id=self.subject, subject_name="Shared", session_id="agent-a:1", source_type="conversation-user", content="once", profile_id="profile-a", workspace_id="project-a", origin_agent_id="agent-a", idempotency_key="agent-a:1:user:1")
        second = insert_raw_event(self.root, subject_id=self.subject, subject_name="Shared", session_id="agent-a:1", source_type="conversation-user", content="changed payload", profile_id="profile-a", workspace_id="project-a", origin_agent_id="agent-a", idempotency_key="agent-a:1:user:1")
        self.assertTrue(first["inserted"])
        self.assertFalse(second["inserted"])
        with self.assertRaises(ValueError):
            ensure_session(self.root, subject_id=self.subject, session_id="", profile_id="profile-a", workspace_id="project-a", shared_mode=True)

    def test_empty_worker_filters_are_never_widened_and_leases_are_exclusive(self) -> None:
        event_id = self.event("A bounded event.", workspace="project-a", agent="agent-a")
        build_cards(self.root, subject_id=self.subject, profile_id="profile-a", workspace_id="project-a", force=True)
        empty = extract_units(self.root, subject_id=self.subject, card_ids=[], profile_id="profile-a", workspace_id="project-a")
        self.assertEqual(empty["card_count"], 0)
        self.assertEqual(build_plan(self.root, self.subject, unit_ids=[], profile_id="profile-a", workspace_id="project-a")["actions"], [])

        conn = open_db(self.root)
        enqueue_projection(conn, entity_type="claim", entity_id="missing", operation="reindex", payload={"event": event_id})
        conn.commit(); conn.close()
        first = _claim_batch(self.root, "worker-a", limit=1)
        second = _claim_batch(self.root, "worker-b", limit=1)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_plain_questions_are_not_extracted_as_facts(self) -> None:
        self.event("我的回答风格偏好是什么？", workspace="project-a", agent="agent-a", session="agent-a:question")
        build_cards(self.root, subject_id=self.subject, profile_id="profile-a", workspace_id="project-a", force=True)
        result = extract_units(self.root, subject_id=self.subject, profile_id="profile-a", workspace_id="project-a")
        self.assertEqual(result["created"], [])
        self.assertTrue(any(item["reason"] == "question_or_nonmemory" for item in result["skipped"]))

    def test_review_jobs_preserve_identity_and_strict_event_range(self) -> None:
        first = self.event("first range event", workspace="project-a", agent="agent-a", session="agent-a:range")
        second = self.event("second range event", workspace="project-a", agent="agent-a", session="agent-a:range")
        job = enqueue_review(self.root, subject_id=self.subject, session_id="agent-a:range", event_start_id=first, event_end_id=first, trigger_type="turn_end", profile_id="profile-a", workspace_id="project-a", origin_agent_id="agent-a")
        result = run_pending(self.root, max_jobs=1)
        self.assertEqual(result["results"][0]["job_id"], job["job_id"])
        conn = open_db(self.root)
        scope = conn.execute("SELECT profile_id,workspace_id,origin_agent_id FROM review_jobs WHERE job_uid=?", (job["job_id"],)).fetchone()
        unit_ids = conn.execute("SELECT raw_event_id FROM memory_units ORDER BY raw_event_id").fetchall()
        state = conn.execute("SELECT processed_state FROM raw_events WHERE id=?", (second,)).fetchone()[0]
        conn.close()
        self.assertEqual(scope, ("profile-a", "project-a", "agent-a"))
        self.assertEqual(unit_ids, [(first,)])
        self.assertEqual(state, "pending")

    def test_candidate_procedures_never_enter_context(self) -> None:
        learning = create_learning(self.root, subject_id=self.subject, task_class="deploy", instruction_text="candidate procedure", source_event_ids=[], profile_id="profile-a", workspace_id="project-a", visibility_scope="agent", owner_agent_id="agent-a")
        self.assertEqual(retrieve_procedures(self.root, subject_id=self.subject, query="deploy procedure", profile_id="profile-a", workspace_id="project-a", agent_id="agent-a"), [])
        conn = open_db(self.root)
        conn.execute("UPDATE procedural_learnings SET status='approved', prompt_eligible=1 WHERE learning_uid=?", (learning["learning_id"],))
        conn.commit(); conn.close()
        self.assertEqual(len(retrieve_procedures(self.root, subject_id=self.subject, query="deploy procedure", profile_id="profile-a", workspace_id="project-a", agent_id="agent-a")), 1)
        self.assertEqual(retrieve_procedures(self.root, subject_id=self.subject, query="deploy procedure", profile_id="profile-a", workspace_id="project-a", agent_id="agent-b"), [])

    def test_api_token_binds_profile_workspace_and_agent(self) -> None:
        config = self.root / "agents.json"
        config.write_text(json.dumps({"agents": {"writer": {"token_env": "META_TEST_TOKEN", "profile_id": "profile-a", "agent_id": "agent-a", "workspaces": ["project-a"], "permissions": ["record"]}}}), encoding="utf-8")
        previous = os.environ.get("META_TEST_TOKEN")
        os.environ["META_TEST_TOKEN"] = "secret"
        try:
            principal = authorize(load_principals(config), "Bearer secret", permission="record", workspace_id="project-a")
            self.assertEqual(identity({"workspace_id": "project-a"}, principal), ("profile-a", "project-a", "agent-a"))
            with self.assertRaises(PermissionError):
                identity({"workspace_id": "project-a", "agent_id": "other-agent"}, principal)
            with self.assertRaises(PermissionError):
                authorize(load_principals(config), "Bearer secret", permission="record", workspace_id="project-b")
        finally:
            if previous is None:
                os.environ.pop("META_TEST_TOKEN", None)
            else:
                os.environ["META_TEST_TOKEN"] = previous


if __name__ == "__main__":
    unittest.main()
