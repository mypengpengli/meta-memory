from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from meta_memory.config import AppConfig
from meta_memory.config_commands import get_config_value, set_config_value
from meta_memory.dream_heartbeat import dream_status, run_heartbeat
from meta_memory.runtime import after, before

from meta_memory.legacy import bootstrap

bootstrap()
from _common import open_db


class DreamHeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = AppConfig(path=self.root / "config.toml", user_name="Ada", user_id="ada", store=self.root / "store")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_heartbeat_refreshes_completed_work_and_old_session_moves_at_next_before(self) -> None:
        first_b = before(
            self.config,
            query="Agent B starts before the shared change.",
            session="agent-b-session",
            project_name="heartbeat",
            start=self.root,
            agent_id="agent-b",
            turn_uid="agent-b-first",
        )
        after(self.config, turn_uid="agent-b-first", assistant_text="Agent B initial answer.", agent_id="agent-b")
        before(
            self.config,
            query="The heartbeat project now uses generation-two-shared-memory.",
            session="agent-a-session",
            project_name="heartbeat",
            start=self.root,
            agent_id="agent-a",
            turn_uid="agent-a-change",
        )
        after(self.config, turn_uid="agent-a-change", assistant_text="The shared change is complete.", agent_id="agent-a")

        first = run_heartbeat(self.config)
        self.assertEqual(first["status"], "ok")
        self.assertGreaterEqual(first["processed_turns"], 1)
        conn = open_db(self.config.store)
        try:
            state = conn.execute(
                "SELECT hot_generation FROM workspace_runtime_state WHERE profile_id=? AND workspace_id=? AND subject_id=? AND agent_id=''",
                (self.config.profile_id, "project:heartbeat", self.config.subject_id),
            ).fetchone()
            prior_session_generation = conn.execute("SELECT hot_generation FROM sessions WHERE session_id=?", (first_b["internal_session_id"],)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(state)
        self.assertLess(int(prior_session_generation[0] or 0), int(state[0] or 0))

        resumed_b = before(
            self.config,
            query="What changed in the heartbeat project?",
            session="agent-b-session",
            project_name="heartbeat",
            start=self.root,
            agent_id="agent-b",
            turn_uid="agent-b-resumed",
        )
        self.assertIn("generation-two-shared-memory", resumed_b["hot_context"] + resumed_b["context"])
        conn = open_db(self.config.store)
        try:
            current_generation = conn.execute("SELECT hot_generation FROM sessions WHERE session_id=?", (resumed_b["internal_session_id"],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(int(current_generation[0] or 0), int(state[0] or 0))

        idle = run_heartbeat(self.config)
        self.assertEqual(idle["status"], "idle")
        status = dream_status(self.config)
        self.assertEqual(status["heartbeat"]["last_status"], "idle")

    def test_config_supports_documented_heartbeat_intervals(self) -> None:
        for value in ("1", "10", "1000", "1440", "10080"):
            updated = set_config_value(self.config, "dream.heartbeat_interval_minutes", value)
            self.assertEqual(updated["value"], int(value))
        self.assertEqual(get_config_value(self.config, "dream.heartbeat_interval_minutes")["value"], 10080)
        with self.assertRaises(ValueError):
            set_config_value(self.config, "dream.heartbeat_interval_minutes", "0")


if __name__ == "__main__":
    unittest.main()
