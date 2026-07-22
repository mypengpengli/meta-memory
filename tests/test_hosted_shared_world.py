from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from meta_memory.config import AppConfig
from meta_memory.http_api import APIServer, Principal
from meta_memory.remote_client import RemoteConfig, RemoteMemoryClient
from meta_memory.shared_memory import (
    ensure_audience,
    ensure_channel,
    grant_audience_member,
)


class HostedSharedWorldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        audience = ensure_audience(
            self.root,
            profile_id="family-profile",
            audience_type="household",
            audience_key="home",
            profile_wide=False,
        )
        channel = ensure_channel(
            self.root,
            profile_id="family-profile",
            channel_type="household",
            channel_key="home",
            audience_id=str(audience["audience_id"]),
        )
        self.audience_id = str(audience["audience_id"])
        self.channel_id = str(channel["channel_id"])
        for agent in ("home-robot", "family-planner"):
            grant_audience_member(
                self.root,
                profile_id="family-profile",
                audience_id=self.audience_id,
                member_type="agent",
                member_id=agent,
            )
        common_permissions = frozenset(
            {"turns", "status", "read", "record", "remember", "shared", "assets", "maps", "spatial"}
        )
        principals = {
            "robot-token": Principal(
                profile_id="family-profile",
                agent_id="home-robot",
                workspaces=frozenset({"home-robot-workspace"}),
                permissions=common_permissions,
                subject_ids=frozenset({"person:owner", "person:child"}),
                audiences=frozenset({self.audience_id, self.channel_id}),
            ),
            "planner-token": Principal(
                profile_id="family-profile",
                agent_id="family-planner",
                workspaces=frozenset({"family-dashboard"}),
                permissions=common_permissions,
                subject_ids=frozenset({"person:owner", "person:child"}),
                audiences=frozenset({self.audience_id, self.channel_id}),
            ),
        }
        config = AppConfig(
            path=self.root / "config.toml",
            user_name="Family",
            user_id="server-default",
            store=self.root,
            maintenance_enabled=False,
            dream_enabled=False,
        )
        self.server = APIServer(
            ("127.0.0.1", 0),
            self.root,
            principals,
            config=config,
            asset_chunk_bytes=128 * 1024,
            max_asset_bytes=16 * 1024 * 1024,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.old_robot_token = os.environ.get("META_MEMORY_TEST_ROBOT_TOKEN")
        self.old_planner_token = os.environ.get("META_MEMORY_TEST_PLANNER_TOKEN")
        os.environ["META_MEMORY_TEST_ROBOT_TOKEN"] = "robot-token"
        os.environ["META_MEMORY_TEST_PLANNER_TOKEN"] = "planner-token"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.old_robot_token is None:
            os.environ.pop("META_MEMORY_TEST_ROBOT_TOKEN", None)
        else:
            os.environ["META_MEMORY_TEST_ROBOT_TOKEN"] = self.old_robot_token
        if self.old_planner_token is None:
            os.environ.pop("META_MEMORY_TEST_PLANNER_TOKEN", None)
        else:
            os.environ["META_MEMORY_TEST_PLANNER_TOKEN"] = self.old_planner_token
        self.temp.cleanup()

    def client(self, *, planner: bool = False) -> RemoteMemoryClient:
        name = "family-planner" if planner else "home-robot"
        workspace = "family-dashboard" if planner else "home-robot-workspace"
        token_env = "META_MEMORY_TEST_PLANNER_TOKEN" if planner else "META_MEMORY_TEST_ROBOT_TOKEN"
        return RemoteMemoryClient(
            RemoteConfig(
                url=self.url,
                token_env=token_env,
                agent_id=name,
                workspace_id=workspace,
                subject_id="person:owner",
                audience_id=self.audience_id,
                channel_id=self.channel_id,
                outbox_dir=self.root / f"outbox-{name}",
                timeout_seconds=10,
            )
        )

    def test_cross_workspace_activity_state_and_before_context(self) -> None:
        robot = self.client()
        now = datetime.now(timezone.utc)
        observed = now.isoformat()
        short_expiry = (now + timedelta(minutes=15)).isoformat()
        robot.publish_activity(
            {
                "summary": "The refrigerator is not cooling.",
                "title": "Appliance needs repair",
                "activity_kind": "household",
                "occurred_at": "2026-07-22T08:00:00+00:00",
                "valid_until": "2026-07-30T08:00:00+00:00",
            }
        )
        robot.publish_state(
            {
                "subject_id": "person:child",
                "state_key": "last_seen",
                "summary": "Child last seen by the playground entrance.",
                "value": {"location": "playground entrance"},
                "source_ref": "robot-camera:event-42",
                "observed_at": observed,
                "valid_until": short_expiry,
                "confidence": 0.93,
            }
        )

        planner = self.client(planner=True)
        before = planner.before(
            "What important things happened at home?",
            session_id="planner-conversation-1",
            turn_id="planner-turn-1",
        )
        shared = before["shared_context"]
        self.assertEqual(shared["counts"]["activities"], 1)
        self.assertEqual(shared["counts"]["states"], 1)
        self.assertIn("refrigerator", shared["activities"][0]["summary"])
        self.assertEqual(shared["states"][0]["subject_id"], "person:child")
        child_states = planner._request(  # Exercise the public JSON route with an explicit family subject.
            "GET",
            "/v1/states",
            query={
                **planner.config.identity(require_session=False),
                "subject_id": "person:child",
                "state_key": "last_seen",
            },
        )
        self.assertEqual(child_states["states"][0]["value"]["location"], "playground entrance")

    def test_resumable_asset_map_observation_search_and_download(self) -> None:
        robot = self.client()
        source = self.root / "room-scan.bin"
        source.write_bytes((b"room-image-and-map\x00" * 20000) + b"tail")
        uploaded = robot.upload_asset(
            source,
            media_type="application/octet-stream",
            metadata={"captured_at": "2026-07-22T09:00:00+00:00", "sensor": "home-robot"},
        )
        asset = uploaded["asset"]
        self.assertGreater(int(asset["byte_size"]), self.server.asset_chunk_bytes)

        mapped = robot.map(
            "put",
            payload={
                "map_id": "home-floor-1",
                "coordinate_frame": "map",
                "asset_id": asset["asset_id"],
                "captured_at": "2026-07-22T09:00:00+00:00",
                "name": "Home first floor",
                "idempotency_key": "map:home-floor-1:v1",
            },
        )
        self.assertEqual(mapped["map"]["version"], 1)
        observed = robot.observe(
            {
                "content": "Water is visible under the kitchen sink.",
                "observed_at": "2026-07-22T09:01:00+00:00",
                "valid_until": "2026-07-23T09:01:00+00:00",
                "source_ref": "home-robot:vision-7",
                "map_id": "home-floor-1",
                "asset_ids": [asset["asset_id"]],
                "location_id": "kitchen-sink",
                "location_text": "Kitchen, under the sink",
                "objects": [{"label": "water", "confidence": 0.96}],
                "confidence": 0.94,
            }
        )
        self.assertEqual(observed["observation"]["map_id"], "home-floor-1")

        planner = self.client(planner=True)
        found = planner._request(
            "GET",
            "/v1/spatial-observations",
            query={**planner.config.identity(require_session=False), "query": "water sink"},
        )
        self.assertEqual(len(found["observations"]), 1)
        self.assertEqual(found["observations"][0]["asset_uri"], f"meta-memory://assets/{asset['asset_id']}")

        output = self.root / "downloaded.bin"
        downloaded = planner.download_asset(str(asset["asset_id"]), output)
        self.assertEqual(downloaded["sha256"], asset["sha256"])
        self.assertEqual(output.read_bytes(), source.read_bytes())

        receipt_files = list((self.root / "outbox-home-robot" / "uploads").glob("*.json"))
        self.assertEqual(len(receipt_files), 1)
        self.assertNotIn("robot-token", receipt_files[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
