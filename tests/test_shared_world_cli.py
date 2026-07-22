from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from meta_memory.cli import build_parser, dispatch
from meta_memory.config import AppConfig, save_config


class SharedWorldCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = AppConfig(
            path=self.root / "config.toml",
            user_name="CLI User",
            user_id="cli-user",
            store=self.root / "store",
            maintenance_enabled=False,
            dream_enabled=False,
            dream_heartbeat_enabled=False,
            dream_deep_enabled=False,
        )
        save_config(self.config)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *arguments: str):
        args = build_parser().parse_args(["--config", str(self.config.path), "--json", *arguments])
        return dispatch(args)

    def test_shared_activity_state_and_overview_are_operable(self) -> None:
        created = self.run_cli(
            "shared", "init", "--type", "household", "--key", "home", "--label", "Family home",
            "--member-agent", "home-robot",
        )
        channel = str(created["channel"]["channel_id"])
        self.run_cli(
            "shared", "publish", "--channel-id", channel,
            "--summary", "The refrigerator is not cooling.",
        )
        self.run_cli(
            "shared", "state-set", "--channel-id", channel,
            "--subject-id", "person:child", "--state-key", "last_seen",
            "--summary", "Last seen at playground.", "--source-ref", "robot:event-1",
            "--valid-until", "2099-01-01T00:00:00+00:00",
        )
        feed = self.run_cli("shared", "feed", "--channel-id", channel)
        states = self.run_cli("shared", "states", "--channel-id", channel, "--subject-id", "person:child")
        self.assertEqual(len(feed["activities"]), 1)
        self.assertEqual(states["states"][0]["state_key"], "last_seen")

        overview = self.run_cli("overview", "--project", "general", "--cwd", str(self.root))
        self.assertEqual(overview["counts"]["shared_activities"], 1)
        self.assertEqual(overview["counts"]["current_states"], 1)

    def test_asset_map_and_spatial_commands_form_one_practical_flow(self) -> None:
        created = self.run_cli("shared", "init", "--type", "household", "--key", "home")
        channel = str(created["channel"]["channel_id"])
        image = self.root / "room.jpg"
        image.write_bytes(b"fake-room-image" * 100)
        metadata = self.root / "asset.json"
        metadata.write_text(json.dumps({"captured_at": "2026-07-22T10:00:00+00:00"}), encoding="utf-8")
        added = self.run_cli(
            "asset", "add", str(image), "--media-type", "image/jpeg", "--metadata-file", str(metadata),
        )
        asset_id = str(added["asset"]["asset_id"])
        mapped = self.run_cli(
            "map", "add", "--channel-id", channel, "--map-id", "home-floor-1",
            "--coordinate-frame", "map", "--asset-id", asset_id,
        )
        self.assertEqual(mapped["map"]["version"], 1)
        observed = self.run_cli(
            "spatial", "add", "--channel-id", channel, "--map-id", "home-floor-1",
            "--asset-id", asset_id, "--location-text", "kitchen",
            "--caption", "Water under the sink", "--observed-at", "2026-07-22T10:01:00+00:00",
        )
        self.assertTrue(observed["observation"]["observation_id"])
        found = self.run_cli("spatial", "search", "water sink", "--channel-id", channel)
        self.assertEqual(len(found["observations"]), 1)
        exported = self.root / "copy.jpg"
        self.run_cli("asset", "export", asset_id, "--output", str(exported))
        self.assertEqual(exported.read_bytes(), image.read_bytes())


if __name__ == "__main__":
    unittest.main()
