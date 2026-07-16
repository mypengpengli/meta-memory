from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from meta_memory.cli import _setup, build_parser
from meta_memory.config import AppConfig
from meta_memory.skill_installer import _launcher_command, _launcher_shell_help, install_agent


class SetupResilienceTests(unittest.TestCase):
    def test_windows_launcher_help_covers_powershell_cmd_and_process_apis(self) -> None:
        launcher = Path(r"C:\Agent Files\it's\meta-memory-demo.cmd")
        self.assertEqual(
            _launcher_command(launcher, windows=True),
            r"& 'C:\Agent Files\it''s\meta-memory-demo.cmd'",
        )
        help_text = _launcher_shell_help(launcher, windows=True)
        self.assertIn("Host process/argv API", help_text)
        self.assertIn("PowerShell", help_text)
        self.assertIn("cmd.exe", help_text)
        self.assertIn("Git Bash", help_text)

    def test_failed_launcher_probe_is_not_reported_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(
                path=root / "config.toml",
                user_name="Ada",
                user_id="ada",
                store=root / "store",
            )
            with patch(
                "meta_memory.skill_installer._verify_launcher",
                return_value=(False, "error", "simulated launcher failure"),
            ):
                result = install_agent(
                    "custom",
                    config=config,
                    custom_agent_id="portable-agent",
                    custom_skill_dir=root / "skills",
                    no_host_file=True,
                )

            self.assertEqual(result["status"], "needs_action")
            self.assertFalse(result["launcher_verified"])
            self.assertEqual(result["next_action"], "meta-memory agent verify portable-agent")
            self.assertTrue(any("simulated launcher failure" in item for item in result["warnings"]))

    def test_scheduler_failure_keeps_completed_core_setup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(
                path=root / "config.toml",
                user_name="Ada",
                user_id="ada",
                store=root / "store",
            )
            args = build_parser().parse_args([
                "setup",
                "--name", "Ada",
                "--store", str(config.store),
                "--maintenance", "yes",
                "--dream", "no",
                "--agents", "codex",
                "--non-interactive",
            ])
            installed = [{
                "status": "ok",
                "agent": "codex",
                "skill_installed": True,
                "launcher_verified": True,
            }]

            with (
                patch("meta_memory.skill_installer.install_agents", return_value=installed),
                patch("meta_memory.scheduler.install_schedule", side_effect=RuntimeError("scheduler permission denied")),
            ):
                result = _setup(config, args)

            self.assertTrue(config.path.is_file())
            self.assertEqual(result["agents"], installed)
            self.assertEqual(result["status"], "needs_action")
            self.assertEqual(result["schedule"]["status"], "error")
            self.assertEqual(result["next_action"], "meta-memory schedule install")
            self.assertIn("core runtime", result["warning"])

    def test_setup_never_claims_host_activation_after_file_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(
                path=root / "config.toml",
                user_name="Ada",
                user_id="ada",
                store=root / "store",
            )
            args = build_parser().parse_args([
                "setup",
                "--name", "Ada",
                "--store", str(config.store),
                "--maintenance", "no",
                "--dream", "no",
                "--agents", "codex",
                "--no-schedule",
                "--non-interactive",
            ])
            manual = (
                "Restart or start Codex, complete one normal conversation, then run "
                "meta-memory agent status --all --verbose and confirm lifecycle_state is active."
            )
            installed = [{
                "status": "needs_action",
                "installation_status": "ok",
                "agent": "codex",
                "launcher_verified": True,
                "activation_required": True,
                "next_action": "meta-memory agent status --all --verbose",
                "manual_next_step": manual,
            }]

            with patch("meta_memory.skill_installer.install_agents", return_value=installed):
                result = _setup(config, args)

            self.assertEqual(result["status"], "needs_action")
            self.assertEqual(result["next_action"], "meta-memory agent status --all --verbose")
            self.assertEqual(result["warning"], manual)


if __name__ == "__main__":
    unittest.main()
