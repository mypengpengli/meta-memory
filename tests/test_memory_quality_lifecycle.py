from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from meta_memory.config import AppConfig
from meta_memory.dream import archive_dream_run, list_dream_runs, preview_dream, run_dream, show_dream_run
from meta_memory.legacy import bootstrap
from meta_memory.runtime import after, before, remember
from meta_memory.turn_service import abandon_turn, complete_late_turn, reopen_turn, touch_turn

bootstrap()
from _common import open_db
from build_session_card import build_rolling_state
from extract_memory_units import route_memory_entry, structured_fields


class MemoryQualityLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = AppConfig(path=self.root / "config.toml", user_name="Ada", user_id="ada", store=self.root / "store")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_entry_gate_preserves_stable_workflow_and_filters_turn_chatter(self) -> None:
        self.assertEqual(route_memory_entry("继续")["retention_class"], "ignore")
        self.assertEqual(route_memory_entry("帮我提交主分支")["retention_class"], "session_only")
        self.assertEqual(route_memory_entry("Please commit the main branch now.")["retention_class"], "session_only")
        routed = route_memory_entry("以后这个项目修改后直接提交并推送远端")
        self.assertEqual(routed["retention_class"], "long_term_candidate")
        self.assertEqual(routed["reason"], "stable_workflow_or_preference")
        fields = structured_fields("以后这个项目修改后直接提交并推送远端", "", "")
        self.assertEqual(fields["predicate"], "procedure")
        self.assertEqual(fields["subject_text"], "workflow")

    def test_structured_session_state_keeps_legacy_summary_separate(self) -> None:
        state = build_rolling_state(
            [
                {"source_type": "conversation-user", "content": "本项目的目标是完成记忆系统升级。"},
                {"source_type": "conversation-assistant", "content": "已完成迁移并决定采用增量检索。下一步优化命令。"},
            ],
            open_questions=["如何查看待审核记忆？"],
            updated_at="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual(state["goal"], "本项目的目标是完成记忆系统升级。")
        self.assertTrue(state["completed"])
        self.assertTrue(state["decisions"])
        self.assertTrue(state["next_steps"])
        self.assertEqual(state["open_questions"], ["如何查看待审核记忆？"])

    def test_dream_empty_store_is_true_idle_preview(self) -> None:
        preview = preview_dream(self.config)
        self.assertEqual(preview["status"], "preview")
        self.assertFalse(preview["would_run"])
        result = run_dream(self.config)
        self.assertEqual(result["status"], "idle")
        conn = open_db(self.config.store)
        try:
            self.assertEqual(int(conn.execute("SELECT COUNT(*) FROM dream_runs").fetchone()[0]), 0)
            self.assertEqual(int(conn.execute("SELECT COUNT(*) FROM dream_nodes").fetchone()[0]), 0)
        finally:
            conn.close()

    def test_changed_dream_is_source_linked_then_becomes_idle_and_archiveable(self) -> None:
        saved = remember(
            self.config,
            content="From now on, prefer concise release notes for this project.",
            project_name="dream-project",
            start=self.root,
        )
        self.assertEqual(saved["status"], "ok")
        preview = preview_dream(self.config)
        self.assertTrue(preview["would_run"])
        first = run_dream(self.config)
        self.assertEqual(first["status"], "ok")
        self.assertTrue(first["report"])
        self.assertTrue(first["nodes"])
        self.assertTrue(all(node["prompt_eligible"] for node in first["nodes"] if node["type"] == "project_digest"))
        self.assertEqual(run_dream(self.config)["status"], "idle")
        listed = list_dream_runs(self.config)
        self.assertEqual(listed["runs"][0]["run_id"], first["run_id"])
        shown = show_dream_run(self.config, run_id=first["run_id"])
        self.assertTrue(shown["reports"])
        archived = archive_dream_run(self.config, run_id=first["run_id"])
        self.assertTrue(archived["archived"])

    def test_before_injects_completed_cross_agent_continuity_only_on_resume_intent(self) -> None:
        first = before(
            self.config,
            query="Implement the continuity marker for this workspace.",
            session="agent-a-session",
            project_name="continuity-project",
            start=self.root,
            agent_id="agent-a",
            turn_uid="continuity-a",
        )
        after(self.config, turn_uid=first["turn_id"], assistant_text="Completed the continuity marker implementation.", agent_id="agent-a")
        from build_session_card import build_cards

        build_cards(
            self.config.store,
            subject_id=self.config.subject_id,
            profile_id=self.config.profile_id,
            workspace_id="project:continuity-project",
            origin_agent_id="agent-a",
            force=True,
        )
        resumed = before(
            self.config,
            query="Please continue from the previous work.",
            session="agent-b-session",
            project_name="continuity-project",
            start=self.root,
            agent_id="agent-b",
            turn_uid="continuity-b",
        )
        summaries = resumed["cross_agent_continuity"]["sessions"]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["origin_agent_id"], "agent-a")
        self.assertIn("Workspace Continuity", resumed["context"])

    def test_late_completion_and_reopen_are_explicit_lifecycle_paths(self) -> None:
        # Build just enough durable state to exercise service APIs without
        # coupling this test to the retrieval layer.
        conn = open_db(self.config.store)
        try:
            conn.execute(
                """
                INSERT INTO turns(turn_uid,profile_id,workspace_id,subject_id,origin_agent_id,external_session_id,
                                  internal_session_id,user_event_id,status,context_status,started_at,last_active_at,updated_at)
                VALUES('late-turn', ?, 'project:test', ?, 'agent-a', 'session-a', 'internal-a', 1,
                       'started', 'ready', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (self.config.profile_id, self.config.subject_id),
            )
            conn.execute(
                """
                INSERT INTO raw_events(subject_id,subject_name,session_id,source_type,content,content_hash,profile_id,workspace_id,origin_agent_id,visibility_scope,turn_uid,message_role)
                VALUES(?, 'Ada', 'session-a', 'conversation-user', 'late request', 'late-request-hash', ?, 'project:test', 'agent-a', 'workspace', 'late-turn', 'user')
                """,
                (self.config.subject_id, self.config.profile_id),
            )
            conn.commit()
        finally:
            conn.close()
        abandon_turn(self.config, turn_uid="late-turn")
        reopened = reopen_turn(self.config, turn_uid="late-turn", agent_id="agent-a")
        self.assertTrue(reopened["reopened"])
        self.assertTrue(touch_turn(self.config, turn_uid="late-turn", agent_id="agent-a")["renewed"])
        abandon_turn(self.config, turn_uid="late-turn")
        late = complete_late_turn(self.config, turn_uid="late-turn", assistant_text="late response", agent_id="agent-a")
        self.assertTrue(late["late_completion"])


if __name__ == "__main__":
    unittest.main()
