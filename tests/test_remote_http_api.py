from __future__ import annotations

import http.client
import hashlib
import json
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlencode

from meta_memory.config import AppConfig
from meta_memory.http_api import APIServer, Principal, load_principals
from meta_memory.shared_memory import ensure_audience, ensure_channel, grant_audience_member


class RemoteHTTPAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.principal = Principal(
            profile_id="profile-remote",
            agent_id="robot-home",
            workspaces=frozenset({"household:home"}),
            permissions=frozenset({"read", "record", "remember", "feedback", "proposals"}),
            subject_ids=frozenset({"person:owner", "person:child"}),
            audiences=frozenset({"audience:family", "channel:household"}),
        )
        config = AppConfig(
            path=self.root / "config.toml",
            user_name="Remote owner",
            user_id="server-default",
            store=self.root,
            maintenance_enabled=False,
            dream_enabled=False,
            dream_heartbeat_enabled=False,
            dream_deep_enabled=False,
        )
        self.server = APIServer(("127.0.0.1", 0), self.root, {"test-secret": self.principal}, config=config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        token: str = "test-secret",
    ) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        headers = {"Authorization": f"Bearer {token}"}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        parsed = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, parsed

    def turn_payload(self, turn_id: str, **extra: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "profile_id": "profile-remote",
            "agent_id": "robot-home",
            "workspace_id": "household:home",
            "subject_id": "person:owner",
            "session_id": "robot-home:conversation-1",
            "turn_id": turn_id,
            "audience_id": "audience:family",
            "channel_id": "channel:household",
        }
        payload.update(extra)
        return payload

    def test_health_and_complete_remote_turn_lifecycle(self) -> None:
        status, health = self.request("GET", "/healthz", token="")
        self.assertEqual(status, 200)
        self.assertEqual(health, {"status": "ok"})

        turn_id = str(uuid.uuid4())
        status, before = self.request(
            "POST",
            "/v1/turns/before",
            self.turn_payload(turn_id, query="客厅空调现在有什么异常？"),
        )
        self.assertEqual(status, 200, before)
        self.assertIn(before["status"], {"ok", "degraded"})
        self.assertEqual(before["turn_id"], turn_id)
        self.assertEqual(before["workspace_id"], "household:home")
        self.assertEqual(before["subject_id"], "person:owner")
        self.assertNotIn("project_root", before)

        status, touched = self.request(
            "POST", f"/v1/turns/{turn_id}/touch", self.turn_payload(turn_id, note="vision task still running")
        )
        self.assertEqual(status, 200, touched)
        self.assertTrue(touched["renewed"])

        query = urlencode({"workspace_id": "household:home", "subject_id": "person:owner"})
        status, agent = self.request("GET", f"/v1/agent/status?{query}")
        self.assertEqual(status, 200, agent)
        self.assertEqual(agent["agent"]["lifecycle_state"], "before_only")
        self.assertEqual(agent["agent"]["pending_turns"], 1)

        answer = "截至本次检查，空调无法启动；建议安排维修。"
        status, after = self.request(
            "POST", f"/v1/turns/{turn_id}/after", self.turn_payload(turn_id, assistant=answer)
        )
        self.assertEqual(status, 200, after)
        self.assertFalse(after["idempotent"])
        self.assertEqual(after["answer_sha256"], hashlib.sha256(answer.encode("utf-8")).hexdigest())

        status, repeated = self.request(
            "POST", f"/v1/turns/{turn_id}/after", self.turn_payload(turn_id, assistant=answer)
        )
        self.assertEqual(status, 200, repeated)
        self.assertTrue(repeated["idempotent"])

        status, agent = self.request("GET", f"/v1/agents/status?{query}")
        self.assertEqual(status, 200, agent)
        self.assertEqual(agent["agent"]["lifecycle_state"], "active")
        self.assertTrue(agent["agent"]["active"])
        self.assertEqual(agent["agent"]["last_before"], agent["agent"]["last_before_at"])
        self.assertEqual(agent["agent"]["last_after"], agent["agent"]["last_after_at"])
        self.assertEqual(agent["agent"]["pending_turns"], 0)
        self.assertEqual(agent["agent"]["turn_counts"]["completed"], 1)

    def test_token_scope_rejects_identity_subject_audience_and_turn_crossing(self) -> None:
        cases = [
            self.turn_payload(str(uuid.uuid4()), query="x", agent_id="other-agent"),
            self.turn_payload(str(uuid.uuid4()), query="x", workspace_id="project:other"),
            self.turn_payload(str(uuid.uuid4()), query="x", subject_id="person:stranger"),
            self.turn_payload(str(uuid.uuid4()), query="x", channel_id="channel:private"),
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                status, result = self.request("POST", "/v1/turns/before", payload)
                self.assertEqual(status, 403, result)

        from _common import open_db

        conn = open_db(self.root)
        rejected_ids = [str(payload["turn_id"]) for payload in cases]
        placeholders = ",".join("?" for _ in rejected_ids)
        persisted = conn.execute(
            f"SELECT COUNT(*) FROM turns WHERE turn_uid IN ({placeholders})",
            rejected_ids,
        ).fetchone()[0]
        conn.close()
        self.assertEqual(persisted, 0)

        turn_id = str(uuid.uuid4())
        status, result = self.request(
            "POST", "/v1/turns/before", self.turn_payload(turn_id, query="bounded request")
        )
        self.assertEqual(status, 200, result)
        wrong_session = self.turn_payload(turn_id, session_id="robot-home:different", assistant="must not save")
        status, result = self.request("POST", f"/v1/turns/{turn_id}/after", wrong_session)
        self.assertEqual(status, 403, result)

        status, result = self.request(
            "POST", f"/v1/turns/{turn_id}/after", self.turn_payload("different", assistant="must not save")
        )
        self.assertEqual(status, 400, result)

    def test_recovery_replay_late_completes_the_same_turn(self) -> None:
        turn_id = str(uuid.uuid4())
        status, result = self.request(
            "POST", "/v1/turns/before", self.turn_payload(turn_id, query="remember this interrupted request")
        )
        self.assertEqual(status, 200, result)

        from _common import open_db

        conn = open_db(self.root)
        conn.execute("UPDATE turns SET status='abandoned' WHERE turn_uid=?", (turn_id,))
        conn.commit()
        conn.close()

        status, replayed = self.request(
            "POST",
            "/v1/recovery/replay",
            self.turn_payload(turn_id, assistant="Recovered exact buffered response."),
        )
        self.assertEqual(status, 200, replayed)
        self.assertTrue(replayed["replayed"])
        self.assertTrue(replayed["late_completion"])

        conn = open_db(self.root)
        row = conn.execute("SELECT status,completion_kind FROM turns WHERE turn_uid=?", (turn_id,)).fetchone()
        conn.close()
        self.assertEqual(row, ("completed_late", "late"))

    def test_agents_file_keeps_old_schema_and_supports_optional_bounds(self) -> None:
        path = self.root / "agents.json"
        path.write_text(
            json.dumps(
                {
                    "agents": {
                        "old": {
                            "token_env": "META_MEMORY_HTTP_OLD_TEST",
                            "profile_id": "old-profile",
                            "agent_id": "old-agent",
                            "workspaces": ["old-workspace"],
                            "permissions": ["read", "record"],
                        },
                        "bounded": {
                            "token_env": "META_MEMORY_HTTP_BOUNDED_TEST",
                            "profile_id": "bounded-profile",
                            "agent_id": "bounded-agent",
                            "workspaces": ["household:home"],
                            "permissions": ["turns", "status"],
                            "subject_ids": ["person:owner"],
                            "audiences": ["audience:family", "channel:household"],
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        old = os.environ.get("META_MEMORY_HTTP_OLD_TEST")
        bounded = os.environ.get("META_MEMORY_HTTP_BOUNDED_TEST")
        os.environ["META_MEMORY_HTTP_OLD_TEST"] = "old-token"
        os.environ["META_MEMORY_HTTP_BOUNDED_TEST"] = "bounded-token"
        try:
            principals = load_principals(path)
        finally:
            if old is None:
                os.environ.pop("META_MEMORY_HTTP_OLD_TEST", None)
            else:
                os.environ["META_MEMORY_HTTP_OLD_TEST"] = old
            if bounded is None:
                os.environ.pop("META_MEMORY_HTTP_BOUNDED_TEST", None)
            else:
                os.environ["META_MEMORY_HTTP_BOUNDED_TEST"] = bounded
        self.assertEqual(principals["old-token"].subject_ids, frozenset())
        self.assertEqual(principals["old-token"].audiences, frozenset())
        self.assertEqual(principals["bounded-token"].subject_ids, frozenset({"person:owner"}))
        self.assertEqual(
            principals["bounded-token"].audiences,
            frozenset({"audience:family", "channel:household"}),
        )

    def test_shared_writes_keep_zero_importance_and_bound_state_subject(self) -> None:
        audience = ensure_audience(
            self.root,
            profile_id=self.principal.profile_id,
            audience_type="household",
            audience_key="http-family",
            profile_wide=False,
        )
        channel = ensure_channel(
            self.root,
            profile_id=self.principal.profile_id,
            channel_type="household",
            channel_key="http-family",
            audience_id=str(audience["audience_id"]),
        )
        grant_audience_member(
            self.root,
            profile_id=self.principal.profile_id,
            audience_id=str(audience["audience_id"]),
            member_type="agent",
            member_id=self.principal.agent_id,
        )
        self.server.tokens["test-secret"] = Principal(
            profile_id=self.principal.profile_id,
            agent_id=self.principal.agent_id,
            workspaces=self.principal.workspaces,
            permissions=self.principal.permissions,
            subject_ids=self.principal.subject_ids,
            audiences=frozenset({str(audience["audience_id"]), str(channel["channel_id"])}),
        )
        base: dict[str, object] = {
            "workspace_id": "household:home",
            "subject_id": "person:owner",
            "audience_id": str(audience["audience_id"]),
            "channel_id": str(channel["channel_id"]),
        }
        status, activity = self.request(
            "POST",
            "/v1/activities",
            {**base, "summary": "Informational event", "importance": 0, "occurred_at": "2030-01-01T12:00:00Z"},
        )
        self.assertEqual(status, 200, activity)
        self.assertEqual(activity["activity"]["importance"], 0.0)

        status, rejected = self.request(
            "POST",
            "/v1/states",
            {
                "workspace_id": "household:home",
                "audience_id": str(audience["audience_id"]),
                "channel_id": str(channel["channel_id"]),
                "state_subject_id": "person:stranger",
                "state_key": "last_seen",
                "summary": "Must not be accepted",
                "source_ref": "camera:unauthorized",
                "observed_at": "2030-01-01T12:00:00Z",
            },
        )
        self.assertEqual(status, 403, rejected)

    def test_after_rejects_a_declared_hash_that_does_not_match_exact_text(self) -> None:
        turn_id = str(uuid.uuid4())
        status, result = self.request(
            "POST", "/v1/turns/before", self.turn_payload(turn_id, query="exact answer test")
        )
        self.assertEqual(status, 200, result)
        answer = "  preserved answer\r\n"
        status, rejected = self.request(
            "POST",
            f"/v1/turns/{turn_id}/after",
            self.turn_payload(turn_id, assistant_text=answer, answer_sha256="0" * 64),
        )
        self.assertEqual(status, 400, rejected)
        expected = hashlib.sha256(answer.encode("utf-8")).hexdigest()
        status, completed = self.request(
            "POST",
            f"/v1/turns/{turn_id}/after",
            self.turn_payload(turn_id, assistant_text=answer, answer_sha256=expected),
        )
        self.assertEqual(status, 200, completed)
        self.assertEqual(completed["answer_sha256"], expected)


if __name__ == "__main__":
    unittest.main()
