from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from meta_memory.shared_memory import (
    build_shared_context,
    ensure_audience,
    ensure_channel,
    grant_audience_member,
)
from meta_memory.spatial import (
    AssetInUseError,
    AssetTooLargeError,
    create_map_version,
    get_asset,
    get_map,
    get_spatial_observation,
    list_assets,
    list_maps,
    list_spatial_observations,
    read_asset,
    record_spatial_observation,
    remove_asset,
    search_spatial_observations,
    store_asset,
)


class SpatialMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Path(self.temporary.name)
        self.profile = "family-profile"
        self.channel = ensure_channel(
            self.store,
            profile_id=self.profile,
            channel_type="household",
            channel_key="home-spatial",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_asset_streaming_dedup_safe_path_integrity_and_size_limit(self) -> None:
        content = b"not-a-real-image\x00but-binary"
        first = store_asset(
            self.store,
            content,
            profile_id=self.profile,
            media_type="image/jpeg",
            original_name="../../unsafe/room.jpg",
            metadata={"camera": "front"},
        )
        second = store_asset(
            self.store,
            io.BytesIO(content),
            profile_id=self.profile,
            media_type="image/jpeg",
            original_name="retry.jpg",
        )
        self.assertEqual(first["asset_id"], second["asset_id"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["original_name"], "room.jpg")
        self.assertNotIn("room.jpg", first["object_path"])
        self.assertEqual(read_asset(self.store, profile_id=self.profile, asset_id=str(first["asset_id"])), content)
        with self.assertRaises(AssetTooLargeError):
            store_asset(self.store, b"12345", profile_id=self.profile, max_bytes=4)

    def test_physical_dedup_survives_one_profile_removal(self) -> None:
        content = b"shared physical bytes"
        first = store_asset(self.store, content, profile_id=self.profile)
        second = store_asset(self.store, content, profile_id="other-profile")
        removed = remove_asset(self.store, profile_id=self.profile, asset_id=str(first["asset_id"]))
        self.assertFalse(removed["removed_bytes"])
        self.assertEqual(read_asset(self.store, profile_id="other-profile", asset_id=str(second["asset_id"])), content)

    def test_versioned_map_and_searchable_spatial_observation(self) -> None:
        asset = store_asset(
            self.store,
            b"room scan",
            profile_id=self.profile,
            media_type="image/jpeg",
            original_name="children-room.jpg",
        )
        map_one = create_map_version(
            self.store,
            profile_id=self.profile,
            channel_id=str(self.channel["channel_id"]),
            map_id="home-floor-1",
            coordinate_frame="robot-map",
            asset_id=str(asset["asset_id"]),
            source_agent_id="home-robot",
            metadata={"rooms": ["children-room", "hall"]},
            captured_at="2030-01-01T12:00:00Z",
            idempotency_key="map-upload:1",
        )
        retry = create_map_version(
            self.store,
            profile_id=self.profile,
            channel_id=str(self.channel["channel_id"]),
            map_id="ignored-on-retry",
            coordinate_frame="ignored",
            source_agent_id="home-robot",
            idempotency_key="map-upload:1",
        )
        map_two = create_map_version(
            self.store,
            profile_id=self.profile,
            channel_id=str(self.channel["channel_id"]),
            map_id="home-floor-1",
            coordinate_frame="robot-map",
            version=2,
            source_agent_id="home-robot",
            captured_at="2030-01-02T12:00:00Z",
        )
        self.assertEqual(retry["map_version_id"], map_one["map_version_id"])
        self.assertTrue(retry["deduplicated"])
        self.assertEqual(map_two["previous_version_id"], map_one["map_version_id"])
        self.assertEqual(get_map(self.store, profile_id=self.profile, map_id="home-floor-1")["version"], 2)
        self.assertEqual(len(list_maps(self.store, profile_id=self.profile, latest_only=True)), 1)

        observation = record_spatial_observation(
            self.store,
            profile_id=self.profile,
            channel_id=str(self.channel["channel_id"]),
            workspace_id="household:home",
            subject_id="household:home",
            source_agent_id="home-robot",
            map_id="home-floor-1",
            map_version=2,
            asset_id=str(asset["asset_id"]),
            location_id="room:child",
            location_text="儿童房书桌旁",
            caption="蓝色书包在书桌左边，椅子右后腿疑似松动",
            ocr_text="三年级二班",
            objects=[
                {"name": "蓝色书包", "position": "书桌左侧"},
                {"name": "椅子", "state": "右后腿疑似松动"},
            ],
            confidence=0.91,
            observed_at="2030-01-02T13:00:00Z",
            valid_until="2099-01-02T13:00:00Z",
            idempotency_key="scan:200",
        )
        retry_observation = record_spatial_observation(
            self.store,
            profile_id=self.profile,
            channel_id=str(self.channel["channel_id"]),
            source_agent_id="home-robot",
            caption="retry",
            idempotency_key="scan:200",
        )
        self.assertEqual(retry_observation["observation_id"], observation["observation_id"])
        matches = search_spatial_observations(
            self.store,
            profile_id=self.profile,
            channel_id=str(self.channel["channel_id"]),
            query="儿童房 椅子",
            viewer_agent_id="assistant-agent",
            workspace_id="household:home",
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["map_version"], 2)
        self.assertEqual(matches[0]["asset_uri"], asset["uri"])
        context = build_shared_context(
            self.store,
            profile_id=self.profile,
            channel_id=str(self.channel["channel_id"]),
            agent_id="assistant-agent",
            workspace_id="household:home",
        )
        self.assertEqual(context["spatial"][0]["caption"], observation["caption"])
        self.assertNotIn("room scan", str(context))
        with self.assertRaises(AssetInUseError):
            remove_asset(self.store, profile_id=self.profile, asset_id=str(asset["asset_id"]))
        self.assertEqual(
            remove_asset(self.store, profile_id=self.profile, asset_id=str(asset["asset_id"]), force=True)["status"],
            "deleted",
        )
        self.assertIsNone(get_asset(self.store, profile_id=self.profile, asset_id=str(asset["asset_id"])))

    def test_audience_and_record_visibility_are_enforced_for_viewers(self) -> None:
        audience = ensure_audience(
            self.store,
            profile_id=self.profile,
            audience_type="device",
            audience_key="robot-private",
            profile_wide=False,
        )
        channel = ensure_channel(
            self.store,
            profile_id=self.profile,
            channel_type="device",
            channel_key="robot-private",
            audience_id=str(audience["audience_id"]),
            owner_agent_id="home-robot",
        )
        grant_audience_member(
            self.store,
            profile_id=self.profile,
            audience_id=str(audience["audience_id"]),
            member_type="agent",
            member_id="home-robot",
        )
        record_spatial_observation(
            self.store,
            profile_id=self.profile,
            channel_id=str(channel["channel_id"]),
            workspace_id="household:home",
            source_agent_id="home-robot",
            caption="Internal wheel calibration marker",
            visibility_scope="agent",
        )
        self.assertEqual(
            len(list_spatial_observations(
                self.store,
                profile_id=self.profile,
                channel_id=str(channel["channel_id"]),
                viewer_agent_id="home-robot",
                workspace_id="household:home",
            )),
            1,
        )
        self.assertEqual(
            list_spatial_observations(
                self.store,
                profile_id=self.profile,
                channel_id=str(channel["channel_id"]),
                viewer_agent_id="assistant-agent",
                workspace_id="household:home",
            ),
            [],
        )

    def test_spatial_supersession_and_time_filtering(self) -> None:
        first = record_spatial_observation(
            self.store,
            profile_id=self.profile,
            channel_id=str(self.channel["channel_id"]),
            source_agent_id="home-robot",
            caption="A box blocks the hallway.",
            observed_at="2030-01-01T12:00:00Z",
            valid_until="2030-01-01T14:00:00Z",
        )
        second = record_spatial_observation(
            self.store,
            profile_id=self.profile,
            channel_id=str(self.channel["channel_id"]),
            source_agent_id="home-robot",
            caption="The hallway is clear.",
            observed_at="2030-01-01T13:00:00Z",
            valid_until="2030-01-01T15:00:00Z",
            supersedes_observation_id=str(first["observation_id"]),
        )
        current = list_spatial_observations(
            self.store,
            profile_id=self.profile,
            channel_id=str(self.channel["channel_id"]),
            now="2030-01-01T13:30:00Z",
        )
        self.assertEqual([item["observation_id"] for item in current], [second["observation_id"]])
        self.assertEqual(
            list_spatial_observations(
                self.store,
                profile_id=self.profile,
                channel_id=str(self.channel["channel_id"]),
                now="2030-01-01T16:00:00Z",
            ),
            [],
        )

    def test_deduplicated_bytes_keep_separate_asset_scope_metadata(self) -> None:
        other_channel = ensure_channel(
            self.store,
            profile_id=self.profile,
            channel_type="household",
            channel_key="second-home",
        )
        content = b"same-scanned-room"
        first = store_asset(
            self.store,
            content,
            profile_id=self.profile,
            media_type="image/jpeg",
            original_name="kitchen.jpg",
            metadata={"camera": "robot-a"},
            visibility_scope="channel",
            channel_id=str(self.channel["channel_id"]),
            workspace_id="home-a",
            source_agent_id="robot-a",
        )
        second = store_asset(
            self.store,
            content,
            profile_id=self.profile,
            media_type="image/png",
            original_name="hall.png",
            metadata={"camera": "robot-b"},
            visibility_scope="channel",
            channel_id=str(other_channel["channel_id"]),
            workspace_id="home-b",
            source_agent_id="robot-b",
        )
        self.assertEqual(first["asset_id"], second["asset_id"])
        visible_first = get_asset(
            self.store,
            profile_id=self.profile,
            asset_id=str(first["asset_id"]),
            enforce_visibility=True,
            channel_id=str(self.channel["channel_id"]),
            workspace_id="home-a",
            viewer_agent_id="robot-a",
        )
        visible_second = get_asset(
            self.store,
            profile_id=self.profile,
            asset_id=str(first["asset_id"]),
            enforce_visibility=True,
            channel_id=str(other_channel["channel_id"]),
            workspace_id="home-b",
            viewer_agent_id="robot-b",
        )
        self.assertEqual(visible_first["original_name"], "kitchen.jpg")
        self.assertEqual(visible_first["media_type"], "image/jpeg")
        self.assertEqual(visible_first["metadata"], {"camera": "robot-a"})
        self.assertEqual(visible_second["original_name"], "hall.png")
        self.assertEqual(visible_second["media_type"], "image/png")
        self.assertEqual(visible_second["metadata"], {"camera": "robot-b"})
        self.assertIsNone(
            get_asset(
                self.store,
                profile_id=self.profile,
                asset_id=str(first["asset_id"]),
                enforce_visibility=True,
                channel_id="not-bound",
                workspace_id="other",
                viewer_agent_id="other-agent",
            )
        )
        png_rows = list_assets(
            self.store,
            profile_id=self.profile,
            media_type="image/png",
            enforce_visibility=True,
            channel_id=str(other_channel["channel_id"]),
            workspace_id="home-b",
            viewer_agent_id="robot-b",
        )
        self.assertEqual([row["original_name"] for row in png_rows], ["hall.png"])
        remove_asset(self.store, profile_id=self.profile, asset_id=str(first["asset_id"]))
        restored = store_asset(
            self.store,
            content,
            profile_id=self.profile,
            media_type="image/jpeg",
            original_name="new-kitchen.jpg",
            visibility_scope="channel",
            channel_id=str(self.channel["channel_id"]),
            workspace_id="home-a",
            source_agent_id="robot-a",
        )
        self.assertEqual(restored["asset_id"], first["asset_id"])
        self.assertIsNone(
            get_asset(
                self.store,
                profile_id=self.profile,
                asset_id=str(first["asset_id"]),
                enforce_visibility=True,
                channel_id=str(other_channel["channel_id"]),
                workspace_id="home-b",
                viewer_agent_id="robot-b",
            )
        )

    def test_map_identity_cannot_move_between_channels(self) -> None:
        create_map_version(
            self.store,
            profile_id=self.profile,
            channel_id=str(self.channel["channel_id"]),
            map_id="stable-home-map",
            coordinate_frame="map",
        )
        other = ensure_channel(
            self.store,
            profile_id=self.profile,
            channel_type="household",
            channel_key="other-map-channel",
        )
        with self.assertRaisesRegex(ValueError, "another channel"):
            create_map_version(
                self.store,
                profile_id=self.profile,
                channel_id=str(other["channel_id"]),
                map_id="stable-home-map",
                coordinate_frame="map",
            )

    def test_subject_filter_applies_to_search_before_limit_and_get(self) -> None:
        owner = record_spatial_observation(
            self.store,
            profile_id=self.profile,
            channel_id=str(self.channel["channel_id"]),
            workspace_id="home",
            subject_id="person:owner",
            source_agent_id="home-robot",
            caption="blue bag in owner room",
            observed_at="2030-01-01T08:00:00Z",
        )
        for index in range(3):
            record_spatial_observation(
                self.store,
                profile_id=self.profile,
                channel_id=str(self.channel["channel_id"]),
                workspace_id="home",
                subject_id="person:child",
                source_agent_id="home-robot",
                caption=f"blue bag in child room {index}",
                observed_at=f"2030-01-01T1{index}:00:00Z",
            )
        found = search_spatial_observations(
            self.store,
            profile_id=self.profile,
            channel_id=str(self.channel["channel_id"]),
            query="blue bag",
            subject_id="person:owner",
            limit=1,
        )
        self.assertEqual([row["observation_id"] for row in found], [owner["observation_id"]])
        hidden = get_spatial_observation(
            self.store,
            profile_id=self.profile,
            observation_id=str(owner["observation_id"]),
            channel_id=str(self.channel["channel_id"]),
            subject_ids=["person:child"],
            viewer_subject_ids=["person:child"],
            workspace_id="home",
            viewer_agent_id="home-robot",
        )
        self.assertIsNone(hidden)


if __name__ == "__main__":
    unittest.main()
