from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from meta_memory.config import AppConfig, save_config
from meta_memory.http_api import load_principals
from meta_memory.server_config import write_agent_binding
from meta_memory.ux_overview import overview


class ServerConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = AppConfig(
            path=self.root / "config.toml",
            user_name="Server owner",
            user_id="server-owner",
            store=self.root / "store",
            maintenance_enabled=False,
            dream_enabled=False,
            dream_heartbeat_enabled=False,
            dream_deep_enabled=False,
        )
        save_config(self.config)
        self.previous = os.environ.get("META_MEMORY_SERVER_CONFIG_TEST")
        os.environ["META_MEMORY_SERVER_CONFIG_TEST"] = "same-token-on-both-machines"

    def tearDown(self) -> None:
        if self.previous is None:
            os.environ.pop("META_MEMORY_SERVER_CONFIG_TEST", None)
        else:
            os.environ["META_MEMORY_SERVER_CONFIG_TEST"] = self.previous
        self.temporary.cleanup()

    def test_generated_agents_file_is_immediately_loadable_and_extendable(self) -> None:
        target = self.root / "agents.json"
        result = write_agent_binding(
            target,
            profile_id=self.config.profile_id,
            agent_id="home-robot",
            token_env="META_MEMORY_SERVER_CONFIG_TEST",
            workspaces=["home", "home"],
            subject_ids=["person:owner", "person:child"],
            audiences=["audience:family", "channel:home"],
        )
        self.assertEqual(result["status"], "ok")
        principal = load_principals(target)["same-token-on-both-machines"]
        self.assertEqual(principal.agent_id, "home-robot")
        self.assertEqual(principal.workspaces, frozenset({"home"}))
        self.assertEqual(principal.subject_ids, frozenset({"person:owner", "person:child"}))
        with self.assertRaisesRegex(ValueError, "already exists"):
            write_agent_binding(
                target,
                profile_id=self.config.profile_id,
                agent_id="home-robot",
                token_env="META_MEMORY_SERVER_CONFIG_TEST",
                workspaces=["home"],
                subject_ids=["person:owner"],
            )

    def test_server_overview_does_not_require_a_local_agent_or_schedule(self) -> None:
        target = self.root / "agents.json"
        write_agent_binding(
            target,
            profile_id=self.config.profile_id,
            agent_id="remote-agent",
            token_env="META_MEMORY_SERVER_CONFIG_TEST",
            workspaces=["stable-workspace"],
            subject_ids=["person:owner"],
        )
        result = overview(
            self.config,
            project_name="server",
            start=self.root,
            server=True,
            agents_file=target,
        )
        self.assertEqual(result["mode"], "hosted_server")
        self.assertEqual(result["readiness"]["hosted_server"]["status"], "ready")
        self.assertNotEqual(result["status"], "needs_setup")


if __name__ == "__main__":
    unittest.main()
