from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from meta_memory.shared_memory import (
    audience_ids_for_member,
    build_shared_context,
    ensure_audience,
    ensure_channel,
    expire_time_bounded,
    get_current_state,
    grant_audience_member,
    list_activity_feed,
    list_channels,
    list_temporal_states,
    publish_activity,
    publish_temporal_state,
    revoke_audience_member,
)


class SharedWorldMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Path(self.temporary.name)
        self.profile = "family-profile"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def channel(self) -> dict[str, object]:
        return ensure_channel(
            self.store,
            profile_id=self.profile,
            channel_type="household",
            channel_key="home",
            label="Our home",
        )

    def test_migration_is_additive_and_registers_all_shared_world_tables(self) -> None:
        channel = self.channel()
        db = self.store / "db" / "memory_index.sqlite"
        conn = sqlite3.connect(db)
        try:
            versions = {str(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")}
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            conn.close()
        self.assertIn("024", versions)
        self.assertTrue(
            {
                "memory_audiences",
                "memory_audience_members",
                "memory_channels",
                "shared_activities",
                "temporal_states",
                "binary_assets",
                "spatial_maps",
                "spatial_observations",
            }.issubset(tables)
        )
        self.assertEqual(channel["channel_type"], "household")

    def test_explicit_audience_membership_can_be_profile_wide_or_narrow(self) -> None:
        wide = ensure_audience(
            self.store,
            profile_id=self.profile,
            audience_type="user",
            audience_key="me",
        )
        self.assertIn(
            wide["audience_id"],
            audience_ids_for_member(
                self.store,
                profile_id=self.profile,
                member_type="agent",
                member_id="any-agent",
            ),
        )

        narrow = ensure_audience(
            self.store,
            profile_id=self.profile,
            audience_type="person",
            audience_key="child-a",
            profile_wide=False,
        )
        channel = ensure_channel(
            self.store,
            profile_id=self.profile,
            channel_type="person",
            channel_key="child-a-status",
            audience_id=str(narrow["audience_id"]),
        )
        self.assertEqual(
            list_channels(
                self.store,
                profile_id=self.profile,
                member_type="agent",
                member_id="home-robot",
            ),
            [item for item in list_channels(
                self.store,
                profile_id=self.profile,
                member_type="agent",
                member_id="home-robot",
            ) if item["channel_id"] != channel["channel_id"]],
        )
        grant_audience_member(
            self.store,
            profile_id=self.profile,
            audience_id=str(narrow["audience_id"]),
            member_type="agent",
            member_id="home-robot",
        )
        visible = list_channels(
            self.store,
            profile_id=self.profile,
            member_type="agent",
            member_id="home-robot",
        )
        self.assertIn(channel["channel_id"], {item["channel_id"] for item in visible})
        self.assertTrue(
            revoke_audience_member(
                self.store,
                profile_id=self.profile,
                audience_id=str(narrow["audience_id"]),
                member_type="agent",
                member_id="home-robot",
            )
        )

    def test_curated_activity_feed_crosses_workspaces_and_is_idempotent(self) -> None:
        channel_id = str(self.channel()["channel_id"])
        first = publish_activity(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            source_workspace_id="project:meta-memory",
            source_agent_id="coding-agent",
            summary="Completed the remote memory design.",
            occurred_at="2030-01-01T09:00:00Z",
            idempotency_key="turn-1:activity",
        )
        retry = publish_activity(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            source_workspace_id="project:meta-memory",
            source_agent_id="coding-agent",
            summary="A retry must not duplicate this.",
            occurred_at="2030-01-01T09:01:00Z",
            idempotency_key="turn-1:activity",
        )
        publish_activity(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            source_workspace_id="household:home",
            source_agent_id="home-robot",
            activity_kind="device-alert",
            summary="The living-room air conditioner did not start.",
            occurred_at="2030-01-01T10:00:00Z",
        )
        self.assertEqual(retry["activity_id"], first["activity_id"])
        self.assertTrue(retry["deduplicated"])
        feed = list_activity_feed(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            now="2030-01-01T12:00:00Z",
        )
        self.assertEqual(len(feed), 2)
        self.assertEqual(
            {item["source_workspace_id"] for item in feed},
            {"project:meta-memory", "household:home"},
        )

    def test_temporal_state_supersedes_newer_only_and_expires(self) -> None:
        channel_id = str(self.channel()["channel_id"])
        old = publish_temporal_state(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            subject_id="person:child-a",
            state_key="last_seen_at",
            value={"place": "school gate"},
            summary="Observed at the school gate.",
            source_agent_id="home-robot",
            observed_at="2030-01-01T17:00:00Z",
            valid_until="2030-01-01T19:00:00Z",
            idempotency_key="camera:100",
        )
        current = publish_temporal_state(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            subject_id="person:child-a",
            state_key="last_seen_at",
            value={"place": "home entrance"},
            summary="Observed at home.",
            source_agent_id="home-robot",
            observed_at="2030-01-01T18:00:00Z",
            valid_until="2030-01-01T20:00:00Z",
        )
        late = publish_temporal_state(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            subject_id="person:child-a",
            state_key="last_seen_at",
            value={"place": "playground"},
            source_agent_id="home-robot",
            observed_at="2030-01-01T17:30:00Z",
            valid_until="2030-01-01T19:30:00Z",
        )
        retry = publish_temporal_state(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            subject_id="person:child-a",
            state_key="last_seen_at",
            value={"ignored": True},
            source_agent_id="home-robot",
            observed_at="2030-01-01T17:00:00Z",
            valid_until="2030-01-01T19:00:00Z",
            idempotency_key="camera:100",
        )
        self.assertEqual(retry["state_id"], old["state_id"])
        self.assertEqual(late["status"], "superseded")
        selected = get_current_state(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            subject_id="person:child-a",
            state_key="last_seen_at",
            now="2030-01-01T18:30:00Z",
        )
        self.assertEqual(selected["state_id"], current["state_id"])
        history = list_temporal_states(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            current_only=False,
        )
        self.assertEqual(len(history), 3)
        counts = expire_time_bounded(
            self.store,
            profile_id=self.profile,
            now="2030-01-01T21:00:00Z",
        )
        self.assertEqual(counts["states"], 1)
        self.assertIsNone(
            get_current_state(
                self.store,
                profile_id=self.profile,
                channel_id=channel_id,
                subject_id="person:child-a",
                state_key="last_seen_at",
                now="2030-01-01T21:00:00Z",
            )
        )

    def test_context_builder_is_bounded_and_semantic_only(self) -> None:
        channel_id = str(self.channel()["channel_id"])
        for index in range(5):
            publish_activity(
                self.store,
                profile_id=self.profile,
                channel_id=channel_id,
                source_agent_id="agent-a",
                summary=("activity " + str(index) + " ") * 100,
                occurred_at=f"2030-01-01T0{index}:00:00Z",
            )
        context = build_shared_context(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            agent_id="any-agent",
            limits={"activities": 2, "states": 0, "spatial": 0, "characters": 1500},
        )
        self.assertLessEqual(context["counts"]["activities"], 2)
        self.assertNotIn("bytes", str(context).casefold())

    def test_existing_channel_cannot_be_rebound_to_another_audience(self) -> None:
        channel = self.channel()
        other = ensure_audience(
            self.store,
            profile_id=self.profile,
            audience_type="household",
            audience_key="other-home",
        )
        with self.assertRaisesRegex(ValueError, "cannot be rebound"):
            ensure_channel(
                self.store,
                profile_id=self.profile,
                channel_type="household",
                channel_key="home",
                audience_id=str(other["audience_id"]),
            )
        self.assertNotEqual(channel["audience_id"], other["audience_id"])

    def test_scheduled_state_does_not_retire_current_early_and_activates_when_due(self) -> None:
        channel_id = str(self.channel()["channel_id"])
        current = publish_temporal_state(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            subject_id="person:child-a",
            state_key="location",
            value={"place": "home"},
            observed_at="2030-01-01T10:00:00Z",
            valid_until="2030-01-01T13:00:00Z",
        )
        scheduled = publish_temporal_state(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            subject_id="person:child-a",
            state_key="location",
            value={"place": "school"},
            observed_at="2030-01-01T10:30:00Z",
            valid_from="2030-01-01T12:00:00Z",
            valid_until="2030-01-01T18:00:00Z",
        )
        self.assertEqual(scheduled["status"], "scheduled")
        intermediate = publish_temporal_state(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            subject_id="person:child-a",
            state_key="location",
            value={"place": "car"},
            observed_at="2030-01-01T11:00:00Z",
            valid_until="2030-01-01T13:00:00Z",
        )
        self.assertEqual(intermediate["status"], "active")
        before_due = get_current_state(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            subject_id="person:child-a",
            state_key="location",
            now="2030-01-01T11:00:00Z",
        )
        self.assertEqual(before_due["state_id"], intermediate["state_id"])
        counts = expire_time_bounded(
            self.store,
            profile_id=self.profile,
            now="2030-01-01T12:01:00Z",
        )
        self.assertEqual(counts["states_activated"], 1)
        after_due = get_current_state(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            subject_id="person:child-a",
            state_key="location",
            now="2030-01-01T12:01:00Z",
        )
        self.assertEqual(after_due["state_id"], scheduled["state_id"])

    def test_subject_filter_is_applied_before_feed_limit(self) -> None:
        channel_id = str(self.channel()["channel_id"])
        publish_activity(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            subject_id="person:owner",
            summary="Owner-specific older item",
            occurred_at="2030-01-01T08:00:00Z",
        )
        for index in range(3):
            publish_activity(
                self.store,
                profile_id=self.profile,
                channel_id=channel_id,
                subject_id="person:child",
                summary=f"Child item {index}",
                occurred_at=f"2030-01-01T1{index}:00:00Z",
            )
        rows = list_activity_feed(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            subject_id="person:owner",
            now="2030-01-02T00:00:00Z",
            limit=1,
        )
        self.assertEqual([row["summary"] for row in rows], ["Owner-specific older item"])

    def test_late_state_stays_historical_after_newer_state_expires(self) -> None:
        channel_id = str(self.channel()["channel_id"])
        newer = publish_temporal_state(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            subject_id="person:child-a",
            state_key="last_seen",
            value={"place": "home"},
            observed_at="2030-01-01T18:00:00Z",
            valid_until="2030-01-01T19:00:00Z",
        )
        expire_time_bounded(self.store, profile_id=self.profile, now="2030-01-01T20:00:00Z")
        late = publish_temporal_state(
            self.store,
            profile_id=self.profile,
            channel_id=channel_id,
            subject_id="person:child-a",
            state_key="last_seen",
            value={"place": "school"},
            observed_at="2030-01-01T17:00:00Z",
            valid_until="2030-01-01T21:00:00Z",
        )
        self.assertEqual(newer["status"], "active")
        self.assertEqual(late["status"], "superseded")


if __name__ == "__main__":
    unittest.main()
