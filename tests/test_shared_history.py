from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from meta_memory.config import AppConfig
from meta_memory.runtime import after, before, history

from meta_memory.legacy import bootstrap

bootstrap()
from build_session_card import build_cards
from ingest_raw_event import insert_raw_event


class SharedHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = AppConfig(path=self.root / "config.toml", user_name="Ada", user_id="ada", store=self.root / "store")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_completed_workspace_summaries_are_shared_but_detail_is_bounded(self) -> None:
        user_text = "migration-summary-signal " + ("long-user-detail-" * 80)
        before(
            self.config,
            query=user_text,
            session="agent-a-session",
            project_name="shared-history",
            start=self.root,
            agent_id="agent-a",
            turn_uid="agent-a-turn",
        )
        after(
            self.config,
            turn_uid="agent-a-turn",
            assistant_text="The migration was completed and the tests passed.",
            agent_id="agent-a",
        )
        insert_raw_event(
            self.config.store,
            subject_id=self.config.subject_id,
            subject_name=self.config.user_name,
            session_id="agent-a-session",
            source_type="tool-result",
            source_ref="pytest",
            content="pytest conclusion: passed. Authorization: Bearer secret-tool-output",
            profile_id=self.config.profile_id,
            workspace_id="project:shared-history",
            origin_agent_id="agent-a",
            turn_uid="agent-a-turn",
            message_role="tool",
        )
        build_cards(
            self.config.store,
            subject_id=self.config.subject_id,
            profile_id=self.config.profile_id,
            workspace_id="project:shared-history",
            origin_agent_id="agent-a",
            force=True,
        )

        summary = history(
            self.config,
            query="migration-summary-signal",
            project_name="shared-history",
            start=self.root,
            agent_id="agent-b",
        )
        self.assertEqual(summary["mode"], "summary")
        self.assertEqual(len(summary["sessions"]), 1)
        card = summary["sessions"][0]
        self.assertEqual(card["origin_agent_id"], "agent-a")
        self.assertTrue(card["internal_session_id"])
        self.assertNotIn(user_text, json.dumps(summary))
        self.assertNotIn("secret-tool-output", json.dumps(summary))

        detail = history(
            self.config,
            query="migration-summary-signal",
            project_name="shared-history",
            start=self.root,
            agent_id="agent-b",
            detail=True,
        )
        self.assertEqual(detail["mode"], "detail")
        messages = detail["details"][0]["turns"][0]["messages"]
        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        self.assertIn("migration-summary-signal", messages[0]["content"])
        self.assertNotIn("secret-tool-output", json.dumps(detail))

    def test_unfinished_turns_are_not_shared_and_owner_remains_enforced(self) -> None:
        before(
            self.config,
            query="unfinished-history-signal",
            session="agent-a-session",
            project_name="shared-history",
            start=self.root,
            agent_id="agent-a",
            turn_uid="unfinished-turn",
        )
        build_cards(self.config.store, subject_id=self.config.subject_id, profile_id=self.config.profile_id, workspace_id="project:shared-history", force=True)
        result = history(self.config, query="unfinished-history-signal", project_name="shared-history", start=self.root, agent_id="agent-b")
        self.assertEqual(result["sessions"], [])
        with self.assertRaisesRegex(ValueError, "different Agent"):
            after(self.config, turn_uid="unfinished-turn", assistant_text="Agent B cannot finish this.", agent_id="agent-b")


if __name__ == "__main__":
    unittest.main()
