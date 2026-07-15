from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from meta_memory.cli import build_parser
from meta_memory.config import AppConfig, save_config
from meta_memory.ux_overview import human_text, overview


def _scheduler(*, installed: bool) -> dict[str, object]:
    return {
        "status": "ok",
        "platform": "test",
        "launcher_exists": installed,
        "expected": ["maintain", "dream"],
        "tasks": [
            {"action": "maintain", "installed": installed},
            {"action": "dream", "installed": installed},
        ],
    }


def _agent(*, installed: bool) -> dict[str, object]:
    return {
        "agents": [{
            "agent": "codex",
            "installed": installed,
            "skill": "ok" if installed else "not_found",
            "launcher": "ok" if installed else "not_found",
            "shared_config": True,
            "shared_store": True,
        }],
    }


class GuidanceUxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = AppConfig(path=root / "config.toml", user_name="Ada", user_id="ada", store=root / "store")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @patch("meta_memory.scheduler.schedule_status")
    @patch("meta_memory.agent_status.agent_status")
    def test_overview_first_run_offers_setup_only(self, agent_status, schedule_status) -> None:
        agent_status.return_value = _agent(installed=False)
        schedule_status.return_value = _scheduler(installed=False)

        result = overview(self.config, project_name="demo", start=self.temp.name)

        self.assertEqual(result["status"], "needs_setup")
        self.assertEqual(result["readiness"]["configuration"]["status"], "not_saved")
        self.assertEqual([item["code"] for item in result["actions"]], ["save_initial_setup"])
        self.assertEqual(result["next_action"], "meta-memory setup --agents codex")
        self.assertIn("Save configuration", human_text(result))

    @patch("meta_memory.scheduler.schedule_status")
    @patch("meta_memory.agent_status.agent_status")
    def test_overview_distinguishes_missing_agent_and_schedule(self, agent_status, schedule_status) -> None:
        save_config(self.config)
        agent_status.return_value = _agent(installed=False)
        schedule_status.return_value = _scheduler(installed=False)

        result = overview(self.config, project_name="demo", start=self.temp.name)

        self.assertEqual(result["status"], "needs_setup")
        self.assertEqual(result["readiness"]["agent"]["status"], "not_installed")
        self.assertEqual(result["readiness"]["scheduler"]["status"], "not_installed")
        self.assertEqual([item["code"] for item in result["actions"]], ["install_agent", "install_schedule"])

    @patch("meta_memory.scheduler.schedule_status")
    @patch("meta_memory.agent_status.agent_status")
    def test_overview_ready_state_has_no_fake_repair_action(self, agent_status, schedule_status) -> None:
        save_config(self.config)
        agent_status.return_value = _agent(installed=True)
        schedule_status.return_value = _scheduler(installed=True)

        result = overview(self.config, project_name="demo", start=self.temp.name)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["actions"], [])
        self.assertIn("Ready for normal use", human_text(result))

    def test_top_level_help_leads_with_tasks_and_examples(self) -> None:
        help_text = build_parser().format_help()
        self.assertIn("Commands by task", help_text)
        self.assertIn("Start here:", help_text)
        self.assertIn("meta-memory setup --agents codex", help_text)
        self.assertIn("meta-memory COMMAND --help", help_text)


if __name__ == "__main__":
    unittest.main()
