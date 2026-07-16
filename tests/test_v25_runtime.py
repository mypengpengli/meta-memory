from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from meta_memory.agent_specs import agent_specs
from meta_memory.config import AppConfig, load_config, save_config
from meta_memory.dream import run_dream
from meta_memory.legacy import bootstrap
from meta_memory.maintenance import maintain
from meta_memory.memory_policy import decide_action, normalize_mode
from meta_memory.project_detection import ProjectContext, bind_project, resolve_project
from meta_memory.runtime import before, origin_agent_id, remember
from meta_memory.runtime_locks import acquire, release
from meta_memory.scheduler import _linux_block, _managed_crontab, _windows_task_command
from meta_memory.scheduler_launcher import write_scheduler_launcher
from meta_memory.session_manager import close_session, new_session, resolve_session
from meta_memory.skill_installer import install_agent
from meta_memory.spool import pending_dir, spool_completion


bootstrap()
from _common import open_db
from apply_memory_plan import apply_plan
from ingest_raw_event import insert_raw_event


class SessionIdentityAndPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.project = ProjectContext("Project", "project", self.project_root)
        self.config = AppConfig(
            path=self.root / "config" / "config.toml",
            user_name="Ada",
            user_id="ada",
            store=self.root / "store",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_session_priority_cache_rotation_expiry_and_scope(self) -> None:
        initial = datetime(2026, 7, 14, 1, tzinfo=timezone.utc)
        explicit = resolve_session(
            self.config,
            requested="conversation-42",
            agent_id="Codex",
            project=self.project,
            environ={"META_MEMORY_HOST_SESSION_ID": "host-ignored", "TMUX_PANE": "term-ignored"},
            now=initial,
        )
        self.assertEqual((explicit.session_id, explicit.source, explicit.agent_id), ("conversation-42", "explicit", "codex"))

        host = resolve_session(
            self.config,
            requested="auto",
            agent_id="Codex",
            project=self.project,
            environ={"META_MEMORY_HOST_SESSION_ID": "host-42", "TMUX_PANE": "term-ignored"},
            now=initial,
        )
        self.assertEqual((host.session_id, host.source), ("host-42", "host"))
        self.assertEqual(
            new_session(
                self.config,
                requested="auto",
                agent_id="Codex",
                project=self.project,
                environ={"META_MEMORY_HOST_SESSION_ID": "host-42"},
                now=initial,
            ).session_id,
            "host-42",
        )

        terminal_env = {"TMUX_PANE": "%9"}
        terminal = resolve_session(
            self.config,
            requested="auto",
            agent_id="Codex",
            project=self.project,
            environ=terminal_env,
            parent_pid=101,
            now=initial,
        )
        same_terminal = resolve_session(
            self.config,
            requested="auto",
            agent_id="Codex",
            project=self.project,
            environ=terminal_env,
            parent_pid=101,
            now=initial + timedelta(minutes=1),
        )
        self.assertEqual(terminal.source, "terminal")
        self.assertTrue(same_terminal.reused)
        self.assertEqual(same_terminal.session_id, terminal.session_id)
        self.assertTrue(terminal.cache_path and terminal.cache_path.is_file())

        rotated = new_session(
            self.config,
            requested="auto",
            agent_id="Codex",
            project=self.project,
            environ=terminal_env,
            parent_pid=101,
            now=initial + timedelta(minutes=2),
        )
        self.assertNotEqual(rotated.session_id, terminal.session_id)
        closed = close_session(
            self.config,
            requested="auto",
            agent_id="Codex",
            project=self.project,
            environ=terminal_env,
            parent_pid=101,
        )
        self.assertEqual(closed.session_id if closed else "", rotated.session_id)
        after_close = resolve_session(
            self.config,
            requested="auto",
            agent_id="Codex",
            project=self.project,
            environ=terminal_env,
            parent_pid=101,
            now=initial + timedelta(minutes=3),
        )
        self.assertNotEqual(after_close.session_id, rotated.session_id)

        fallback = resolve_session(
            self.config,
            requested="auto",
            agent_id="Codex",
            project=self.project,
            environ={},
            parent_pid=202,
            now=initial,
        )
        expired = resolve_session(
            self.config,
            requested="auto",
            agent_id="Codex",
            project=self.project,
            environ={},
            parent_pid=202,
            now=initial + timedelta(hours=9),
        )
        other_agent = resolve_session(
            self.config,
            requested="auto",
            agent_id="Claude Code",
            project=self.project,
            environ={},
            parent_pid=202,
            now=initial,
        )
        other_root = self.root / "other-project"
        other_root.mkdir()
        other_project = ProjectContext("Other", "other", other_root)
        other_project_session = resolve_session(
            self.config,
            requested="auto",
            agent_id="Codex",
            project=other_project,
            environ={},
            parent_pid=202,
            now=initial,
        )
        self.assertEqual(fallback.source, "parent")
        self.assertNotEqual(expired.session_id, fallback.session_id)
        self.assertNotEqual(other_agent.session_id, fallback.session_id)
        self.assertNotEqual(other_project_session.session_id, fallback.session_id)

    def test_identity_config_and_agent_installation_are_explicit(self) -> None:
        with patch.dict("os.environ", {"META_MEMORY_AGENT_ID": "from-environment"}, clear=True):
            self.assertEqual(origin_agent_id(), "from-environment")
            self.assertEqual(origin_agent_id("from-cli"), "from-cli")

        self.config.memory_mode = "conservative"
        self.config.maintenance_enabled = False
        self.config.dream_enabled = False
        self.config.session_auto_expire_hours = 12
        save_config(self.config)
        loaded = load_config(self.config.path)
        self.assertEqual(loaded.memory_mode, "conservative")
        self.assertFalse(loaded.maintenance_enabled)
        self.assertFalse(loaded.dream_enabled)
        self.assertEqual(loaded.session_auto_expire_hours, 12)

        legacy = self.root / "legacy.toml"
        legacy.write_text("[behavior]\nauto_memory = false\n", encoding="utf-8")
        self.assertEqual(load_config(legacy).memory_mode, "manual")

        specs = agent_specs(home=self.root / "agent-home")
        self.assertTrue(str(specs["codex"].skill_dir).endswith(".codex\\skills\\meta-memory") or str(specs["codex"].skill_dir).endswith(".codex/skills/meta-memory"))
        installed = install_agent(
            "custom",
            config=self.config,
            custom_agent_id="test-custom",
            custom_skill_dir=self.root / "custom-skills",
            home=self.root / "agent-home",
            verify=False,
        )
        self.assertTrue(installed["skill_installed"])
        self.assertTrue(installed["launcher_created"])
        skill = Path(str(installed["skill"])).read_text(encoding="utf-8")
        self.assertIn("before", skill)
        self.assertIn("after", skill)
        self.assertIn("Retain `turn_id`", skill)
        self.assertIn("after --turn <turn_id>", skill)

        with self.assertRaisesRegex(ValueError, "non-empty --skill-dir"):
            install_agent(
                "custom",
                config=self.config,
                custom_agent_id="blank-path-agent",
                custom_skill_dir="   ",
                verify=False,
            )

    def test_unbound_git_projects_use_a_portable_remote_fingerprint(self) -> None:
        with patch("meta_memory.project_detection.project_root", return_value=self.project_root), patch(
            "meta_memory.project_detection._git_remote_identity", return_value="github.com/example/meta-memory"
        ):
            resolved = resolve_project(self.config, "auto", self.project_root)
        expected = hashlib.sha256(b"github.com/example/meta-memory").hexdigest()[:10]
        self.assertEqual(resolved.project_id, f"repo-{expected}")

    def test_bound_git_project_is_portable_across_cloned_directories(self) -> None:
        original = self.root / "original-checkout"
        restored_clone = self.root / "restored-under-another-name"
        original.mkdir()
        restored_clone.mkdir()
        remote = "github.com/example/meta-memory"
        remote_key = f"remote:{hashlib.sha256(remote.encode('utf-8')).hexdigest()[:16]}"

        with patch(
            "meta_memory.project_detection.project_root",
            side_effect=lambda start=None: Path(start).resolve(),
        ), patch("meta_memory.project_detection._git_remote_identity", return_value=remote):
            bound = bind_project(self.config, "Team Memory", original)
            resolved = resolve_project(self.config, "auto", restored_clone)

        self.assertEqual(bound.project_id, "team-memory")
        self.assertEqual(resolved.project_id, bound.project_id)
        self.assertEqual(resolved.workspace_id, bound.workspace_id)
        self.assertEqual(self.config.projects[str(original.resolve())], bound.project_id)
        self.assertEqual(self.config.projects[remote_key], bound.project_id)
        self.assertNotIn(remote, "\n".join(self.config.projects))

    def test_memory_policy_is_single_gate_for_state_transitions_and_risk(self) -> None:
        safe = {
            "action": "CREATE",
            "source_type": "conversation-user",
            "verification_state": "verified",
            "sensitivity": "normal",
            "confidence": 0.95,
            "prompt_eligible": True,
        }
        self.assertEqual(normalize_mode("unknown"), "automatic")
        self.assertEqual(decide_action(safe, memory_mode="manual"), "stage")
        self.assertEqual(decide_action(safe, memory_mode="conservative"), "auto_apply")
        self.assertEqual(decide_action({**safe, "action": "REFINE"}, memory_mode="conservative"), "stage")
        self.assertEqual(decide_action({**safe, "action": "REFINE"}, memory_mode="automatic"), "stage")
        self.assertEqual(decide_action({**safe, "action": "REFINE", "refine_safe": True}, memory_mode="automatic"), "auto_apply")

        transition = {
            **safe,
            "action": "SUPERSEDE",
            "relation": "REPLACES_OLD_STATE",
            "content": "The service is now migrated to PostgreSQL.",
            # Consolidation plans are generally review-required; automatic
            # mode may relax that generic default only when the evidence is safe.
            "requires_review": True,
        }
        self.assertEqual(decide_action(transition, memory_mode="automatic"), "auto_apply")
        self.assertEqual(decide_action({**transition, "risk_reason": "contradiction"}, memory_mode="automatic"), "stage")
        self.assertEqual(decide_action({**transition, "source_type": "agent-observation"}, memory_mode="automatic"), "stage")
        self.assertEqual(decide_action({**transition, "relation": "CONFLICTS_WITH"}, memory_mode="automatic"), "stage")
        self.assertEqual(decide_action({**transition, "explicit_user_action": True}, memory_mode="manual"), "auto_apply")

    def test_repeated_explicit_remember_keeps_each_user_evidence_record(self) -> None:
        content = "Keep this explicit preference as separately sourced evidence."
        first = remember(
            self.config,
            content=content,
            project_name="explicit",
            start=self.root,
            agent_id="codex",
            scope="project",
        )
        second = remember(
            self.config,
            content=content,
            project_name="explicit",
            start=self.root,
            agent_id="codex",
            scope="project",
        )
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "ok")
        self.assertFalse(first.get("deduplicated", False))
        self.assertFalse(second.get("deduplicated", False))
        conn = open_db(self.config.store)
        try:
            rows = conn.execute(
                "SELECT id,idempotency_key FROM raw_events WHERE source_type='explicit-memory' ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][1], rows[1][1])
        self.assertTrue(all(str(row[1]).startswith("remember:") for row in rows))

    def test_tool_results_are_project_scoped_agent_observations(self) -> None:
        first = remember(
            self.config,
            content="The deployment tool completed migration revision 42.",
            project_name="tooling",
            start=self.root,
            agent_id="codex",
            source_kind="tool-result",
            source_ref="host-call-42",
        )
        second = remember(
            self.config,
            content="The deployment tool completed migration revision 42.",
            project_name="tooling",
            start=self.root,
            agent_id="codex",
            source_kind="tool-result",
            source_ref="host-call-42",
        )
        self.assertEqual(first["status"], "ok")
        self.assertTrue(second.get("deduplicated", False))

        conn = open_db(self.config.store)
        try:
            raw = conn.execute(
                "SELECT source_type,workspace_id,visibility_scope,idempotency_key FROM raw_events WHERE source_ref='host-call-42'"
            ).fetchone()
            claim = conn.execute(
                "SELECT c.memory_kind,c.verification_state,c.workspace_id,c.visibility_scope "
                "FROM claims AS c JOIN claim_sources AS cs ON cs.claim_id=c.id "
                "JOIN raw_events AS r ON r.id=cs.raw_event_id "
                "WHERE r.source_ref=? ORDER BY c.created_at DESC LIMIT 1",
                ("host-call-42",),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(tuple(raw[:3]), ("tool-result", "project:tooling", "workspace"))
        self.assertEqual(str(raw[3]), "tool:host-call-42")
        self.assertNotEqual(claim[0], "profile")
        self.assertEqual(tuple(claim[1:]), ("agent_observed", "project:tooling", "workspace"))

        with self.assertRaisesRegex(ValueError, "project-scoped"):
            remember(
                self.config,
                content="A document says a user preference.",
                project_name="tooling",
                start=self.root,
                source_kind="resource",
                scope="user",
            )


class RuntimeCoordinationAndDreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = AppConfig(
            path=self.root / "config.toml",
            user_name="Ada",
            user_id="ada",
            store=self.root / "store",
        )
        self.subject = self.config.subject_id

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _event(self, content: str, workspace_id: str) -> int:
        return int(
            insert_raw_event(
                self.config.store,
                subject_id=self.subject,
                subject_name=self.config.user_name,
                session_id="test-session",
                source_type="conversation-user",
                content=content,
                profile_id=self.config.profile_id,
                workspace_id=workspace_id,
                origin_agent_id="codex",
                visibility_scope="workspace",
            )["raw_event_id"]
        )

    def _claim(self, event_id: int, content: str, workspace_id: str) -> str:
        action = {
            "plan_id": str(uuid.uuid4()),
            "action": "CREATE",
            "subject_id": self.subject,
            "subject_name": self.config.user_name,
            "source_event_ids": [event_id],
            "memory_kind": "state",
            "domain": "work",
            "topic": "storage",
            "title": content[:50],
            "content": content,
            "predicate": "states",
            "subject_text": "project",
            "object_text": content,
            "confidence": 0.95,
            "importance": 0.9,
            "durability": 0.8,
            "sensitivity": "normal",
            "verification_state": "verified",
            "profile_id": self.config.profile_id,
            "workspace_id": workspace_id,
            "origin_agent_id": "codex",
            "visibility_scope": "workspace",
            "owner_agent_id": "",
        }
        result = apply_plan(
            self.config.store,
            {"schema_version": 3, "subject_id": self.subject, "policy": "automatic", "actions": [action]},
            skip_index=True,
        )
        self.assertEqual(result["results"][0]["status"], "applied")
        return str(result["results"][0]["claim_id"])

    def test_scoped_semantic_claims_keep_distinct_projects_and_mark_runtime_dirty(self) -> None:
        content = "The service uses PostgreSQL."
        first_claim = self._claim(self._event(content, "project:one"), content, "project:one")
        second_claim = self._claim(self._event(content, "project:two"), content, "project:two")
        self.assertNotEqual(first_claim, second_claim)

        conn = open_db(self.config.store)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM claims WHERE subject_id=? AND content=? AND status='active'",
                (self.subject, content),
            ).fetchone()[0]
            indexes = {str(row[1]) for row in conn.execute("PRAGMA index_list(claims)")}
            state = conn.execute(
                "SELECT hot_dirty,dream_dirty,claim_generation FROM workspace_runtime_state WHERE profile_id=? AND workspace_id=? AND subject_id=? AND agent_id=''",
                (self.config.profile_id, "project:one", self.subject),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(count, 2)
        self.assertIn("idx_active_claim_semantic_scope", indexes)
        self.assertEqual(tuple(state[:2]), (1, 1))
        self.assertGreaterEqual(int(state[2]), 1)

    def test_maintenance_is_lock_protected_replays_spool_and_refreshes_hot_memory(self) -> None:
        workspace = "project:maintain"
        self._claim(self._event("The maintenance project uses SQLite.", workspace), "The maintenance project uses SQLite.", workspace)
        held = acquire(self.config.store, f"maintain:{self.config.profile_id}", owner_id="test-owner", lease_seconds=120)
        self.assertTrue(held.acquired)
        skipped = maintain(self.config, max_jobs=2)
        self.assertEqual(skipped["status"], "skipped")
        self.assertTrue(release(self.config.store, held))

        started = before(
            self.config,
            query="Persist this completed answer through the spool.",
            session="spool-session",
            project_name="maintain",
            start=self.root,
            agent_id="codex",
            turn_uid="spool-turn",
        )
        spooled = spool_completion(
            self.config,
            turn_uid=str(started["turn_id"]),
            assistant_text="The deferred answer was saved.",
            error="temporary sqlite lock",
        )
        self.assertTrue(Path(str(spooled["path"])).is_file())
        completed = maintain(self.config, max_jobs=2)
        self.assertEqual(completed["status"], "ok")
        self.assertEqual(completed["spool"]["pending"], 0)
        self.assertFalse(list(pending_dir(self.config).glob("*.json")))

        conn = open_db(self.config.store)
        try:
            turn = conn.execute("SELECT status,assistant_event_id FROM turns WHERE turn_uid='spool-turn'").fetchone()
            state = conn.execute(
                "SELECT hot_dirty,hot_generation,claim_generation FROM workspace_runtime_state WHERE profile_id=? AND workspace_id=? AND subject_id=? AND agent_id=''",
                (self.config.profile_id, workspace, self.subject),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(turn[0], "completed")
        self.assertTrue(turn[1])
        self.assertEqual(state[0], 0)
        self.assertEqual(state[1], state[2])

    def test_dream_nodes_are_scoped_source_linked_and_clear_dream_dirty(self) -> None:
        workspace = "project:dream"
        claim_id = self._claim(
            self._event("The dream project now uses a source-linked digest.", workspace),
            "The dream project now uses a source-linked digest.",
            workspace,
        )
        result = run_dream(self.config, scan_days=1)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(any(Path(path).is_file() for path in result["reports"]))
        self.assertIn(claim_id, result["source_claim_ids"])

        conn = open_db(self.config.store)
        try:
            row = conn.execute(
                "SELECT source_claim_ids,prompt_eligible,status,inference_level FROM dream_nodes WHERE profile_id=? AND workspace_id=? AND subject_id=? AND node_type='project_digest'",
                (self.config.profile_id, workspace, self.subject),
            ).fetchone()
            state = conn.execute(
                "SELECT dream_dirty,dream_generation,claim_generation FROM workspace_runtime_state WHERE profile_id=? AND workspace_id=? AND subject_id=? AND agent_id=''",
                (self.config.profile_id, workspace, self.subject),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertIn(claim_id, json.loads(str(row[0])))
        self.assertEqual(tuple(row[1:]), (1, "active", "extractive"))
        self.assertEqual(state[0], 0)
        self.assertEqual(state[1], state[2])

    def test_scheduler_generation_is_scoped_and_non_destructive(self) -> None:
        self.config.maintenance_interval_minutes = 7
        self.config.dream_heartbeat_interval_minutes = 7
        self.config.dream_schedule = "23:45"
        self.config.dream_deep_schedule = "23:45"
        launcher = self.root / "bin with spaces" / "meta-memory-system.cmd"
        block = _linux_block(self.config, launcher)
        self.assertIn("*/7 * * * *", block)
        self.assertIn("45 23 * * *", block)
        self.assertIn(str(launcher), block)
        managed = _managed_crontab("0 1 * * * unrelated-command\n", block)
        replaced = _managed_crontab(managed, block)
        self.assertIn("unrelated-command", replaced)
        self.assertEqual(replaced.count("# meta-memory:begin"), 1)
        self.assertEqual(replaced.count("# meta-memory:end"), 1)
        self.assertIn('cmd.exe /d /c ""', _windows_task_command(launcher, "maintain"))

        windows_launcher = write_scheduler_launcher(
            self.config,
            windows=True,
            python_executable=self.root / "python.exe",
        )
        text = windows_launcher.read_text(encoding="utf-8")
        self.assertIn("-m meta_memory.cli", text)
        self.assertIn("--agent-id system", text)
        self.assertIn(str(self.config.path.resolve()), text)


if __name__ == "__main__":
    unittest.main()
