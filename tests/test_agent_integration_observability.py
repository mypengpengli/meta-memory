from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from meta_memory.agent_status import agent_status, verify_agent
from meta_memory.config import AppConfig
from meta_memory.runtime import after, before
from meta_memory.skill_installer import install_agent, sync_agent
from meta_memory.ux_overview import _agent_readiness, overview


def _installed_scheduler() -> dict[str, object]:
    return {
        "status": "ok",
        "launcher_exists": True,
        "expected": ["maintain", "dream"],
        "tasks": [
            {"action": "maintain", "installed": True},
            {"action": "dream", "installed": True},
        ],
    }


class AgentIntegrationObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = AppConfig(
            path=self.root / "config.toml",
            user_name="Ada",
            user_id="ada",
            store=self.root / "store",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _install_hermes(self) -> dict[str, object]:
        return install_agent(
            "custom",
            config=self.config,
            custom_agent_id="hermes",
            custom_skill_dir=self.root / "hermes-skills",
            no_host_file=True,
            verify=False,
        )

    def _status(self) -> dict[str, object]:
        return agent_status(
            self.config,
            agent_id="hermes",
            project_name="observe",
            start=self.root,
            verbose=True,
        )

    def _complete_real_turn(self) -> None:
        before(
            self.config,
            query="Please remember the integration activation state.",
            session="integration-observability",
            project_name="observe",
            start=self.root,
            agent_id="hermes",
            turn_uid="integration-observed-turn",
        )
        after(
            self.config,
            turn_uid="integration-observed-turn",
            assistant_text="The host lifecycle has completed once.",
            agent_id="hermes",
        )

    def test_status_separates_install_verify_contract_and_host_lifecycle(self) -> None:
        installed = self._install_hermes()
        self.assertEqual(installed["status"], "needs_action")
        self.assertEqual(installed["installation_status"], "ok")
        self.assertTrue(installed["activation_required"])
        self.assertEqual(installed["next_action"], "meta-memory agent verify hermes")
        registry = Path(str(installed["registry"]))
        initial_registry = json.loads(registry.read_text(encoding="utf-8"))
        self.assertTrue(initial_registry["installed_at"])
        self.assertIsNone(initial_registry["verified_at"])
        self.assertFalse(initial_registry["launcher_verified"])

        initial = self._status()
        self.assertTrue(initial["files_installed"])
        self.assertEqual(initial["host_instruction"], "not_required")
        self.assertTrue(initial["template_contract_current"])
        self.assertEqual(initial["template_contract"]["state"], "current")
        self.assertFalse(initial["launcher_verified"])
        self.assertEqual(initial["launcher_verification"]["state"], "not_checked")
        self.assertEqual(initial["lifecycle_state"], "never_seen")
        self.assertFalse(initial["host_lifecycle_observed"])

        verified = verify_agent(self.config, agent_id="hermes", project_name="observe", start=self.root)
        self.assertEqual(verified["status"], "ok")
        self.assertTrue(verified["launcher_verified"])
        self.assertTrue(verified["activation_required"])
        self.assertIn("does not prove the host", str(verified["verification_scope"]))
        verified_registry = json.loads(registry.read_text(encoding="utf-8"))
        self.assertTrue(verified_registry["verified_at"])
        self.assertTrue(verified_registry["launcher_verified"])

        after_verify = self._status()
        self.assertTrue(after_verify["launcher_verified"])
        self.assertEqual(after_verify["lifecycle_state"], "never_seen")
        self.assertEqual(after_verify["integration_state"], "needs_activation")

        self._complete_real_turn()
        active = self._status()
        self.assertEqual(active["lifecycle_state"], "active")
        self.assertTrue(active["host_lifecycle_observed"])
        self.assertTrue(active["ready_for_automatic_memory"])

        # Sync writes a new integration contract.  The completed turn above
        # predates that contract and must not be mistaken for post-sync host
        # activation.
        sync_agent(self.config, "hermes", verify=False)
        after_sync = self._status()
        self.assertEqual(after_sync["lifecycle_state"], "never_seen")
        self.assertFalse(after_sync["host_lifecycle_observed"])

    def test_template_drift_enters_overview_readiness_without_upgrade_status(self) -> None:
        installed = self._install_hermes()
        verified = verify_agent(self.config, agent_id="hermes", project_name="observe", start=self.root)
        self.assertTrue(verified["launcher_verified"])
        self._complete_real_turn()

        skill = Path(str(installed["skill"]))
        skill.write_text(skill.read_text(encoding="utf-8") + "\nLocal customization that requires sync visibility.\n", encoding="utf-8")

        with patch("meta_memory.scheduler.schedule_status", return_value=_installed_scheduler()):
            result = overview(self.config, project_name="observe", start=self.root)

        self.assertEqual(result["readiness"]["agent"]["status"], "needs_sync")
        self.assertEqual(result["agent"]["id"], "hermes")
        self.assertTrue(result["agent"]["last_after"])
        self.assertIn("hermes", result["readiness"]["agent"]["needs_sync"])
        sync_actions = [item for item in result["actions"] if str(item["code"]) == "sync_agent"]
        self.assertEqual(len(sync_actions), 1)
        self.assertEqual(sync_actions[0]["command"], "meta-memory agent sync --all")

    def test_managed_host_block_drift_is_not_reported_ready(self) -> None:
        installed = install_agent(
            "custom",
            config=self.config,
            custom_agent_id="hosted-agent",
            custom_skill_dir=self.root / "hosted-skills",
            custom_host_file=self.root / "hosted" / "AGENTS.md",
            verify=False,
        )
        host = Path(str(installed["host_instruction"]))
        text = host.read_text(encoding="utf-8")
        host.write_text(text.replace("before` → draft", "skip-before` → draft"), encoding="utf-8")

        value = agent_status(
            self.config,
            agent_id="hosted-agent",
            project_name="observe",
            start=self.root,
            verbose=True,
        )

        self.assertEqual(value["integration_state"], "needs_sync")
        self.assertFalse(value["template_contract_current"])
        self.assertTrue(value["template_contract"]["host_local_drift"])

    def test_launcher_contract_version_participates_in_freshness(self) -> None:
        installed = self._install_hermes()
        registry = Path(str(installed["registry"]))
        payload = json.loads(registry.read_text(encoding="utf-8"))
        payload["launcher_contract_version"] = "launcher-v0"
        registry.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        value = self._status()

        self.assertEqual(value["integration_state"], "needs_sync")
        self.assertTrue(value["template_contract"]["launcher_contract_changed"])

    def test_partial_unregistered_files_request_reinstall_not_sync(self) -> None:
        readiness = _agent_readiness([
            {
                "agent": "codex",
                "installed": False,
                "files_installed": False,
                "installation_state": "partial",
                "skill": "ok",
                "launcher": "not_found",
                "shared_config": True,
                "shared_store": True,
                "template_contract": {"state": "not_installed", "current": False},
            }
        ])

        self.assertEqual(readiness["status"], "needs_install")
        self.assertEqual(readiness["needs_install"], ["codex"])


if __name__ == "__main__":
    unittest.main()
