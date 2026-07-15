from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from meta_memory.agent_status import agent_status, verify_agent
from meta_memory.config import AppConfig
from meta_memory.dream_heartbeat import run_heartbeat
from meta_memory.runtime import after, before
from meta_memory.skill_installer import install_agent

from meta_memory.legacy import bootstrap

bootstrap()
from _common import open_db


class AgentRuntimeStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = AppConfig(path=self.root / "config.toml", user_name="Ada", user_id="ada", store=self.root / "store")
        self.config.turns_unfinished_warning_minutes = 1
        self.config.turns_abandon_after_minutes = 1

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _age_turn(self, turn_uid: str) -> None:
        conn = open_db(self.config.store)
        try:
            conn.execute("UPDATE turns SET started_at='2000-01-01T00:00:00+00:00' WHERE turn_uid=?", (turn_uid,))
            conn.commit()
        finally:
            conn.close()

    def test_before_after_state_warnings_and_abandon_recovery(self) -> None:
        first = before(
            self.config,
            query="runtime-status-user-body-must-not-appear",
            session="status-session",
            project_name="status-project",
            start=self.root,
            agent_id="agent-a",
            turn_uid="status-first",
        )
        after(self.config, turn_uid="status-first", assistant_text="The durable answer is saved.", agent_id="agent-a")
        conn = open_db(self.config.store)
        try:
            row = conn.execute(
                "SELECT last_before_at,last_after_at,last_retrieval_count,total_before,total_after FROM agent_runtime_state WHERE profile_id=? AND agent_id=? AND workspace_id=?",
                (self.config.profile_id, "agent-a", "project:status-project"),
            ).fetchone()
        finally:
            conn.close()
        self.assertTrue(row[0] and row[1])
        self.assertGreaterEqual(int(row[3]), 1)
        self.assertGreaterEqual(int(row[4]), 1)

        before(
            self.config,
            query="old incomplete request",
            session="status-session",
            project_name="status-project",
            start=self.root,
            agent_id="agent-a",
            turn_uid="stale-turn",
        )
        self._age_turn("stale-turn")
        next_turn = before(
            self.config,
            query="new request after a missed completion",
            session="status-session",
            project_name="status-project",
            start=self.root,
            agent_id="agent-a",
            turn_uid="next-turn",
        )
        self.assertTrue(any(isinstance(item, dict) and item.get("code") == "unfinished_previous_turn" for item in next_turn["warnings"]))
        heartbeat = run_heartbeat(self.config)
        self.assertEqual(heartbeat["status"], "ok")
        self.assertIn("stale-turn", heartbeat["turn_recovery"]["abandoned"])
        conn = open_db(self.config.store)
        try:
            stale = conn.execute("SELECT status,last_error FROM turns WHERE turn_uid='stale-turn'").fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(stale), ("abandoned", "after_not_received"))

        report = agent_status(self.config, agent_id="agent-a", project_name="status-project", start=self.root, verbose=True)
        self.assertEqual(report["status"], "ok")
        self.assertNotIn("runtime-status-user-body-must-not-appear", json.dumps(report))
        self.assertIn("details", report)

    def test_project_identity_mismatch_warns_without_merging_and_wrong_owner_is_audited(self) -> None:
        before(
            self.config,
            query="Agent A project identity.",
            session="agent-a-project",
            project_name="project-a",
            start=self.root,
            agent_id="agent-a",
            turn_uid="agent-a-project-turn",
        )
        warning_turn = before(
            self.config,
            query="Agent B has a conflicting project binding.",
            session="agent-b-project",
            project_name="project-b",
            start=self.root,
            agent_id="agent-b",
            turn_uid="agent-b-project-turn",
        )
        self.assertTrue(any(isinstance(item, dict) and item.get("code") == "project_identity_mismatch" for item in warning_turn["warnings"]))
        with self.assertRaisesRegex(ValueError, "different Agent"):
            after(self.config, turn_uid="agent-a-project-turn", assistant_text="wrong agent", agent_id="agent-b")
        conn = open_db(self.config.store)
        try:
            error = conn.execute("SELECT error_code FROM runtime_error_log WHERE turn_uid=? ORDER BY id DESC LIMIT 1", ("agent-a-project-turn",)).fetchone()
            claims = int(conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(error[0], "wrong_turn_owner")
        self.assertEqual(claims, 0)

    def test_agent_verify_checks_a_custom_launcher_without_a_host_login(self) -> None:
        installed = install_agent(
            "custom",
            config=self.config,
            custom_agent_id="hermes",
            custom_skill_dir=self.root / "hermes-skills",
            no_host_file=True,
            verify=False,
        )
        self.assertTrue(installed["launcher_created"])
        verified = verify_agent(self.config, agent_id="hermes", project_name="verify-project", start=self.root)
        self.assertEqual(verified["status"], "ok")
        self.assertTrue(verified["launcher_verified"])
        self.assertTrue(verified["runtime"]["shared_config"])
        self.assertTrue(verified["runtime"]["shared_store"])


if __name__ == "__main__":
    unittest.main()
