from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from meta_memory.config import AppConfig
from meta_memory.skill_installer import install_agent


class CrossAgentLauncherTests(unittest.TestCase):
    def _run_launcher(self, launcher: Path, *args: str) -> dict[str, object]:
        command = [str(launcher), *args]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_custom_agents_have_distinct_launchers_and_one_shared_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = AppConfig(path=root / "config" / "config.toml", user_name="Ada", user_id="ada", store=root / "store")
            agent_a = install_agent(
                "custom",
                config=config,
                custom_agent_id="agent-a",
                custom_skill_dir=root / ".agent-a" / "skills",
                custom_host_file=root / ".agent-a" / "AGENTS.md",
                home=root,
            )
            agent_b = install_agent(
                "custom",
                config=config,
                custom_agent_id="agent-b",
                custom_skill_dir=root / ".agent-b" / "skills",
                no_host_file=True,
                home=root,
            )

            self.assertEqual((agent_a["agent_id"], agent_b["agent_id"]), ("agent-a", "agent-b"))
            self.assertEqual(agent_a["integration_type"], "custom-skill")
            self.assertTrue(agent_a["launcher_verified"])
            self.assertTrue(agent_b["launcher_verified"])
            self.assertEqual(agent_a["shared_config"], agent_b["shared_config"])
            self.assertEqual(agent_a["shared_store"], agent_b["shared_store"])
            self.assertIsNone(agent_b["host_instruction"])
            self.assertTrue(Path(str(agent_a["skill"])).is_file())
            self.assertFalse((root / ".agent-b" / "AGENTS.md").exists())

            status_a = self._run_launcher(Path(str(agent_a["launcher"])), "status")
            status_b = self._run_launcher(Path(str(agent_b["launcher"])), "status")
            self.assertEqual((status_a["status"], status_b["status"]), ("ok", "ok"))

            started = self._run_launcher(
                Path(str(agent_a["launcher"])), "before", "--project", "launcher-shared", "--session", "agent-a-session",
                "--turn", "launcher-agent-a-turn", "--query", "The launcher project now shares cross-agent memory signal.",
            )
            self.assertEqual(started["agent_id"], "agent-a")
            completed = self._run_launcher(
                Path(str(agent_a["launcher"])), "after", "--turn", str(started["turn_id"]), "--assistant", "Agent A completed the shared update.",
            )
            self.assertEqual(completed["status"], "ok")
            heartbeat = self._run_launcher(Path(str(agent_a["launcher"])), "dream", "heartbeat")
            self.assertEqual(heartbeat["status"], "ok")
            agent_b_before = self._run_launcher(
                Path(str(agent_b["launcher"])), "before", "--project", "launcher-shared", "--session", "agent-b-session",
                "--turn", "launcher-agent-b-turn", "--query", "What is the shared cross-agent memory signal?",
            )
            self.assertEqual(agent_b_before["agent_id"], "agent-b")
            self.assertIn("cross-agent memory signal", agent_b_before["hot_context"] + agent_b_before["context"])

    def test_custom_agent_ids_are_strict_and_cannot_impersonate_builtins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = AppConfig(path=Path(temp) / "config.toml", store=Path(temp) / "store")
            for agent_id in ("", "../escape", "system", "meta-memory", "claude_code", "Too Upper"):
                with self.assertRaises(ValueError):
                    install_agent("custom", config=config, custom_agent_id=agent_id, custom_skill_dir=Path(temp) / "skills", verify=False)


if __name__ == "__main__":
    unittest.main()
