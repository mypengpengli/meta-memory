from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from meta_memory.config import AppConfig
from meta_memory.legacy import bootstrap
from meta_memory.maintenance import maintain
from meta_memory.runtime import after, before, history, remember, search

bootstrap()
from _common import open_db
from apply_memory_plan import apply_plan
from build_session_card import build_cards
from ingest_raw_event import insert_raw_event


class V25BoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = AppConfig(
            path=self.root / "config.toml",
            user_name="Ada",
            user_id="ada",
            store=self.root / "store",
        )
        self.subject = self.config.subject_id
        self.profile = self.config.profile_id

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _event(self, content: str, *, source_type: str = "explicit-memory", workspace: str = "project:boundary", agent: str = "codex", visibility: str = "workspace") -> int:
        return int(
            insert_raw_event(
                self.config.store,
                subject_id=self.subject,
                subject_name="Ada",
                session_id="boundary-session",
                source_type=source_type,
                source_ref=f"test:{uuid.uuid4()}",
                content=content,
                profile_id=self.profile,
                workspace_id=workspace,
                origin_agent_id=agent,
                visibility_scope=visibility,
            )["raw_event_id"]
        )

    def _claim(self, content: str, event_id: int, *, memory_kind: str = "state", workspace: str = "project:boundary", agent: str = "codex", visibility: str = "workspace", owner: str = "") -> str:
        action = {
            "plan_id": str(uuid.uuid4()),
            "action": "CREATE",
            "subject_id": self.subject,
            "subject_name": "Ada",
            "source_event_ids": [event_id],
            "memory_kind": memory_kind,
            "domain": "work",
            "topic": "boundary",
            "title": content[:50],
            "content": content,
            "predicate": "states",
            "subject_text": "user" if memory_kind == "profile" else "project",
            "object_text": content,
            "qualifiers": {},
            "confidence": 0.95,
            "importance": 0.9,
            "durability": 0.9,
            "sensitivity": "normal",
            "verification_state": "verified",
            "profile_id": self.profile,
            "workspace_id": workspace,
            "visibility_scope": visibility,
            "owner_agent_id": owner,
            "origin_agent_id": agent,
        }
        outcome = apply_plan(self.config.store, {"schema_version": 3, "subject_id": self.subject, "policy": "automatic", "actions": [action]}, skip_index=True)
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["results"][0]["status"], "applied")
        return str(outcome["results"][0]["claim_id"])

    def test_same_host_session_is_isolated_by_agent_and_current_turn_is_not_recalled(self) -> None:
        codex = before(
            self.config,
            query="codex-only-session-token",
            session="shared-host-session",
            project_name="shared",
            start=self.root,
            agent_id="codex",
            turn_uid="codex-turn",
        )
        self.assertNotIn("codex-only-session-token", codex["context"])
        after(self.config, turn_uid="codex-turn", assistant_text="Codex reply.", agent_id="codex")
        claude = before(
            self.config,
            query="claude-only-session-token",
            session="shared-host-session",
            project_name="shared",
            start=self.root,
            agent_id="claude-code",
            turn_uid="claude-turn",
        )
        self.assertNotIn("claude-only-session-token", claude["context"])
        after(self.config, turn_uid="claude-turn", assistant_text="Claude reply.", agent_id="claude-code")

        cards = build_cards(self.config.store, subject_id=self.subject, profile_id=self.profile, workspace_id="project:shared", force=True)
        self.assertEqual({str(item["origin_agent_id"]) for item in cards["cards"]}, {"codex", "claude-code"})

        codex_history = history(self.config, query="codex-only-session-token", project_name="shared", start=self.root, agent_id="codex")
        claude_history = history(self.config, query="codex-only-session-token", project_name="shared", start=self.root, agent_id="claude-code")
        self.assertEqual(len(codex_history["sessions"]), 1)
        self.assertEqual(len(claude_history["sessions"]), 1)
        self.assertEqual(claude_history["sessions"][0]["origin_agent_id"], "codex")

        self.config.history_scope = "agent"
        isolated = history(self.config, query="codex-only-session-token", project_name="shared", start=self.root, agent_id="claude-code")
        self.assertEqual(isolated["sessions"], [])

    def test_other_agent_cannot_complete_a_turn(self) -> None:
        before(
            self.config,
            query="turn-owner-token",
            session="turn-owner-session",
            project_name="turn-owner",
            start=self.root,
            agent_id="codex",
            turn_uid="owned-turn",
        )
        with self.assertRaisesRegex(ValueError, "different Agent"):
            after(self.config, turn_uid="owned-turn", assistant_text="wrong owner", agent_id="claude-code")
        completed = after(self.config, turn_uid="owned-turn", assistant_text="right owner", agent_id="codex")
        self.assertEqual(completed["status"], "ok")

    def test_automatic_global_preference_reaches_another_project_but_project_rule_does_not(self) -> None:
        self.config.memory_mode = "automatic"
        before(
            self.config,
            query="Please always answer in Chinese.",
            session="scope-session",
            project_name="alpha",
            start=self.root,
            agent_id="codex",
            turn_uid="scope-turn",
        )
        after(self.config, turn_uid="scope-turn", assistant_text="I will do so.", agent_id="codex")
        result = maintain(self.config, max_jobs=10)
        self.assertEqual(result["status"], "ok")
        conn = open_db(self.config.store)
        try:
            global_claim = conn.execute(
                "SELECT memory_kind,workspace_id,visibility_scope,status FROM claims WHERE content=?",
                ("Please always answer in Chinese.",),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(global_claim), ("profile", "global", "global", "active"))
        other_project = search(
            self.config,
            query="Chinese answer preference",
            project_name="beta",
            start=self.root,
            agent_id="claude-code",
        )
        self.assertTrue(any("Please always answer in Chinese." in str(item.get("summary", "")) for item in other_project["results"]))
        local_saved = remember(
            self.config,
            content="For this project, always answer in English.",
            project_name="alpha",
            start=self.root,
            agent_id="codex",
        )
        self.assertEqual(local_saved["status"], "ok")
        conn = open_db(self.config.store)
        try:
            local = conn.execute(
                "SELECT workspace_id,visibility_scope FROM claims WHERE content=?",
                ("For this project, always answer in English.",),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(local), ("project:alpha", "workspace"))

    def test_agent_evidence_cannot_change_profile_and_private_semantics_do_not_merge(self) -> None:
        profile_event = self._event("Ada prefers concise answers.")
        profile_id = self._claim("Ada prefers concise answers.", profile_event, memory_kind="profile")
        observed = self._event("Ada prefers concise answers.", source_type="agent-observation", agent="codex")
        blocked = {
            "plan_id": str(uuid.uuid4()),
            "action": "CORROBORATE",
            "subject_id": self.subject,
            "target_claim_id": profile_id,
            "source_event_ids": [observed],
            "memory_kind": "profile",
            "profile_id": self.profile,
            "workspace_id": "project:boundary",
            "visibility_scope": "workspace",
            "owner_agent_id": "",
            "origin_agent_id": "codex",
            "source_type": "agent-observation",
        }
        outcome = apply_plan(self.config.store, {"schema_version": 3, "subject_id": self.subject, "actions": [blocked]}, skip_index=True)
        self.assertEqual(outcome["status"], "invalid")

        text = "private same-meaning fact"
        left = self._claim(text, self._event(text, agent="agent-a", visibility="agent"), workspace="project:private", agent="agent-a", visibility="agent", owner="agent-a")
        right = self._claim(text, self._event(text, agent="agent-b", visibility="agent"), workspace="project:private", agent="agent-b", visibility="agent", owner="agent-b")
        self.assertNotEqual(left, right)


if __name__ == "__main__":
    unittest.main()
