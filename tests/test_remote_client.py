from __future__ import annotations

import hashlib
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from meta_memory.remote_client import (
    RemoteConfig,
    RemoteConfigurationError,
    RemoteMemoryClient,
    RemoteSemanticError,
    RemoteTransportError,
    main,
)
from meta_memory.remote_installer import install_remote_agent


class RemoteClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.token = "secret-that-must-never-be-persisted"
        self.environment = patch.dict(os.environ, {"TEST_META_MEMORY_TOKEN": self.token}, clear=False)
        self.environment.start()
        self.config = RemoteConfig(
            url="https://memory.example.test",
            token_env="TEST_META_MEMORY_TOKEN",
            agent_id="home-robot",
            workspace_id="household:home",
            subject_id="person:owner",
            audience_id="family:home",
            channel_id="household-events",
            outbox_dir=self.root / "outbox",
        )

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def test_before_after_preserve_identity_exact_text_and_hashes(self) -> None:
        client = RemoteMemoryClient(self.config)
        calls: list[tuple[str, str, dict[str, object]]] = []

        def request(method: str, path: str, payload=None, **_kwargs):
            body = dict(payload or {})
            calls.append((method, path, body))
            if path.endswith("/before"):
                return {"status": "ok", "turn_id": body["turn_id"], "shared_context": "family signal"}
            return {
                "status": "ok", "turn_id": body["turn_id"],
                "answer_sha256": body.get("answer_sha256", ""),
            }

        client._request = request  # type: ignore[method-assign]
        started = client.before("孩子的书包在哪里？", session_id="conversation-42", turn_id="turn-42")
        self.assertEqual(started["shared_context"], "family signal")
        completed = client.after("turn-42", "截至 18:00，机器人最后看到书包在儿童房。")
        self.assertEqual(completed["status"], "ok")

        before = calls[0][2]
        after = calls[1][2]
        self.assertEqual(before["workspace_id"], "household:home")
        self.assertEqual(before["subject_id"], "person:owner")
        self.assertEqual(before["session_id"], "conversation-42")
        self.assertEqual(before["audience_id"], "family:home")
        self.assertEqual(before["channel_id"], "household-events")
        self.assertEqual(after["answer_sha256"], hashlib.sha256(str(after["assistant_text"]).encode("utf-8")).hexdigest())
        self.assertIn(str(after["answer_sha256"]), str(after["idempotency_key"]))
        state = json.loads(client.outbox.state_path("turn-42").read_text(encoding="utf-8"))
        self.assertEqual(state["after_status"], "ok")
        self.assertNotIn(self.token, json.dumps(state, ensure_ascii=False))

    def test_dynamic_turn_scope_survives_receipts_retries_after_and_offline_replay(self) -> None:
        client = RemoteMemoryClient(self.config)
        calls: list[tuple[str, dict[str, object]]] = []

        def acknowledged(_method: str, path: str, payload=None, **_kwargs):
            body = dict(payload or {})
            calls.append((path, body))
            result = {"status": "ok", "turn_id": body.get("turn_id")}
            if not path.endswith("/before"):
                result["answer_sha256"] = body.get("answer_sha256", "")
            return result

        client._request = acknowledged  # type: ignore[method-assign]
        scope = {
            "workspace_id": "household:away",
            "subject_id": "person:child",
            "session_id": "child-conversation",
        }
        client.before("Where is the child?", turn_id="child-turn", **scope)
        # A same-content retry must read the receipt without confusing the
        # configured default subject/workspace with this Turn's exact scope.
        client.before("Where is the child?", turn_id="child-turn", **scope)
        client.after("child-turn", "The child is at the playground entrance.")
        state = client.outbox.load_state("child-turn") or {}
        self.assertEqual(state["workspace_id"], scope["workspace_id"])
        self.assertEqual(state["subject_id"], scope["subject_id"])
        self.assertEqual(state["session_id"], scope["session_id"])
        self.assertEqual(state["config_binding"]["subject_id"], self.config.subject_id)
        for _path, body in calls:
            self.assertEqual(body["workspace_id"], scope["workspace_id"])
            self.assertEqual(body["subject_id"], scope["subject_id"])
            self.assertEqual(body["session_id"], scope["session_id"])

        client._request = lambda *_args, **_kwargs: (_ for _ in ()).throw(RemoteTransportError("offline"))  # type: ignore[method-assign]
        client.before("Remember this trip", turn_id="offline-child-turn", **scope)
        client.after("offline-child-turn", "The family reached the park.")
        replayed: list[tuple[str, dict[str, object]]] = []

        def recovered(_method: str, path: str, payload=None, **_kwargs):
            body = dict(payload or {})
            replayed.append((path, body))
            return {
                "status": "ok", "turn_id": body.get("turn_id"),
                "answer_sha256": body.get("answer_sha256", ""),
            }

        client._request = recovered  # type: ignore[method-assign]
        result = client.replay()
        self.assertEqual(result["status"], "ok")
        self.assertEqual([path for path, _body in replayed], ["/v1/turns/before", "/v1/recovery/replay"])
        for _path, body in replayed:
            self.assertEqual(body["workspace_id"], scope["workspace_id"])
            self.assertEqual(body["subject_id"], scope["subject_id"])
            self.assertEqual(body["session_id"], scope["session_id"])

    def test_cli_process_scope_override_keeps_installed_outbox_binding(self) -> None:
        config_path = self.root / "installed-remote.json"
        config_path.write_text(json.dumps({
            "url": self.config.url,
            "token_env": self.config.token_env,
            "agent_id": self.config.agent_id,
            "workspace_id": self.config.workspace_id,
            "subject_id": self.config.subject_id,
            "audience_id": self.config.audience_id,
            "channel_id": self.config.channel_id,
            "outbox_dir": str(self.config.outbox_dir),
        }), encoding="utf-8")
        # `before` and `after` normally run in separate launcher processes.
        # Only the first process receives the child/workspace/session flags;
        # the second must still open the same installed binding and recover
        # the exact dynamic identity from the Turn receipt.
        first_config = RemoteConfig.load(
            config_path,
            workspace_id="household:away",
            subject_id="person:child",
            session_id="child-chat",
        )
        second_config = RemoteConfig.load(config_path)
        self.assertEqual(first_config.binding(), second_config.binding())
        self.assertEqual(first_config.outbox_dir, second_config.outbox_dir)
        first = RemoteMemoryClient(first_config)
        first._request = lambda _method, _path, payload=None, **_kwargs: {  # type: ignore[method-assign]
            "status": "ok", "turn_id": (payload or {}).get("turn_id")
        }
        first.before("Child request", session_id="child-chat", turn_id="separate-process-turn")

        sent: list[dict[str, object]] = []
        second = RemoteMemoryClient(second_config)

        def completed(_method: str, _path: str, payload=None, **_kwargs):
            body = dict(payload or {})
            sent.append(body)
            return {
                "status": "ok", "turn_id": body.get("turn_id"),
                "answer_sha256": body.get("answer_sha256", ""),
            }

        second._request = completed  # type: ignore[method-assign]
        second.after("separate-process-turn", "Child answer")
        self.assertEqual(sent[0]["workspace_id"], "household:away")
        self.assertEqual(sent[0]["subject_id"], "person:child")
        self.assertEqual(sent[0]["session_id"], "child-chat")

    def test_environment_only_installation_keeps_default_binding_across_scope_override(self) -> None:
        environment = {
            "META_MEMORY_URL": self.config.url,
            "META_MEMORY_AGENT_ID": self.config.agent_id,
            "META_MEMORY_WORKSPACE_ID": self.config.workspace_id,
            "META_MEMORY_SUBJECT_ID": self.config.subject_id,
            "META_MEMORY_AUDIENCE_ID": self.config.audience_id,
            "META_MEMORY_CHANNEL_ID": self.config.channel_id,
            "META_MEMORY_OUTBOX": str(self.root / "environment-outbox"),
            "META_MEMORY_TOKEN": self.token,
        }
        with patch.dict(os.environ, environment, clear=False):
            child = RemoteConfig.load(
                None, workspace_id="household:away", subject_id="person:child", session_id="child-session"
            )
            default = RemoteConfig.load(None)
        self.assertEqual(child.binding(), default.binding())
        self.assertEqual(child.outbox_dir, default.outbox_dir)

    def test_ambiguous_after_is_durable_and_replays_same_answer(self) -> None:
        client = RemoteMemoryClient(self.config)
        client._request = lambda _method, _path, payload=None, **_kwargs: {  # type: ignore[method-assign]
            "status": "ok", "turn_id": (payload or {}).get("turn_id")
        }
        client.before("检查客厅", session_id="robot-run-1", turn_id="robot-turn-1")
        client._request = lambda *_args, **_kwargs: (_ for _ in ()).throw(RemoteTransportError("timeout"))  # type: ignore[method-assign]
        answer = "客厅空调无法启动。"
        deferred = client.after("robot-turn-1", answer)
        self.assertEqual(deferred["status"], "local_outbox")
        records = client.outbox.pending_records(operation="after")
        self.assertEqual(len(records), 1)
        stored = records[0][1]
        self.assertEqual(stored["payload"]["assistant_text"], answer)
        self.assertEqual(stored["answer_sha256"], hashlib.sha256(answer.encode("utf-8")).hexdigest())
        self.assertNotIn(self.token, records[0][0].read_text(encoding="utf-8"))
        with self.assertRaisesRegex(RemoteSemanticError, "different exact answer"):
            client.after("robot-turn-1", "空调已经恢复。")

        sent: list[dict[str, object]] = []

        def recovered(_method: str, _path: str, payload=None, **_kwargs):
            sent.append(dict(payload or {}))
            return {
                "status": "ok", "turn_id": (payload or {}).get("turn_id"),
                "answer_sha256": (payload or {}).get("answer_sha256", ""),
            }

        client._request = recovered  # type: ignore[method-assign]
        replay = client.replay()
        self.assertEqual(replay["status"], "ok")
        self.assertEqual(replay["pending"], 0)
        self.assertEqual(sent[0]["assistant_text"], answer)

    def test_offline_before_and_after_replay_in_protocol_order(self) -> None:
        client = RemoteMemoryClient(self.config)
        client._request = lambda *_args, **_kwargs: (_ for _ in ()).throw(RemoteTransportError("offline"))  # type: ignore[method-assign]
        started = client.before("附近路线有什么变化？", session_id="outing-1", turn_id="outing-turn")
        self.assertEqual(started["status"], "degraded")
        self.assertEqual(started["shared_context"]["counts"], {"activities": 0, "states": 0, "spatial": 0})
        self.assertEqual(started["durability"], "local_outbox")
        completed = client.after("outing-turn", "机器人扫描到东门道路正在施工。")
        self.assertEqual(completed["status"], "local_outbox")

        operations: list[str] = []

        def recovered(_method: str, path: str, payload=None, **_kwargs):
            operations.append(path)
            return {
                "status": "ok", "turn_id": (payload or {}).get("turn_id"),
                "answer_sha256": (payload or {}).get("answer_sha256", ""),
            }

        client._request = recovered  # type: ignore[method-assign]
        result = client.replay()
        self.assertEqual(result["pending"], 0)
        self.assertEqual(operations[:2], ["/v1/turns/before", "/v1/recovery/replay"])

    def test_semantic_failure_is_not_misclassified_as_retryable(self) -> None:
        client = RemoteMemoryClient(self.config)
        client._request = lambda _method, _path, payload=None, **_kwargs: {  # type: ignore[method-assign]
            "status": "ok", "turn_id": (payload or {}).get("turn_id")
        }
        client.before("hello", session_id="session", turn_id="semantic-turn")
        client._request = lambda *_args, **_kwargs: (_ for _ in ()).throw(RemoteSemanticError("wrong owner", status_code=409))  # type: ignore[method-assign]
        with self.assertRaisesRegex(RemoteSemanticError, "wrong owner"):
            client.after("semantic-turn", "answer")
        self.assertEqual(client.outbox.pending_count(), 1)
        self.assertTrue(client.outbox.pending_records()[0][1]["blocked"])

    def test_many_concurrent_turns_get_isolated_receipts(self) -> None:
        client = RemoteMemoryClient(self.config)
        client._request = lambda _method, _path, payload=None, **_kwargs: {  # type: ignore[method-assign]
            "status": "ok", "turn_id": (payload or {}).get("turn_id"),
            "answer_sha256": (payload or {}).get("answer_sha256", ""),
        }

        def one(number: int) -> str:
            turn = f"parallel-{number}"
            client.before(f"request {number}", session_id=f"session-{number % 3}", turn_id=turn)
            client.after(turn, f"answer {number}")
            return turn

        with ThreadPoolExecutor(max_workers=8) as pool:
            turns = list(pool.map(one, range(24)))
        receipts = list((self.root / "outbox" / "turns").glob("*.json"))
        self.assertEqual(len(receipts), len(turns))
        stored = [json.loads(path.read_text(encoding="utf-8")) for path in receipts]
        self.assertEqual({item["turn_id"] for item in stored}, set(turns))
        self.assertEqual({item["answer_sha256"] for item in stored}, {
            hashlib.sha256(f"answer {number}".encode()).hexdigest() for number in range(24)
        })

    def test_config_rejects_literal_token_and_accepts_token_env_name(self) -> None:
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({
            "url": "https://memory.example.test", "token": self.token,
            "agent_id": "agent", "workspace_id": "workspace", "subject_id": "subject",
        }), encoding="utf-8")
        with self.assertRaisesRegex(RemoteConfigurationError, "must not contain a token"):
            RemoteConfig.load(bad)

    def test_explicit_config_pins_origin_identity_and_outbox_against_ambient_environment(self) -> None:
        config_path = self.root / "pinned.json"
        pinned_outbox = self.root / "pinned-outbox"
        config_path.write_text(json.dumps({
            "url": "https://pinned.example.test",
            "token_env": "TEST_META_MEMORY_TOKEN",
            "agent_id": "pinned-agent",
            "workspace_id": "pinned-workspace",
            "subject_id": "pinned-subject",
            "outbox_dir": str(pinned_outbox),
            "timeout_seconds": 11,
        }), encoding="utf-8")
        with patch.dict(os.environ, {
            "META_MEMORY_URL": "https://ambient.example.test",
            "META_MEMORY_AGENT_ID": "ambient-agent",
            "META_MEMORY_WORKSPACE_ID": "ambient-workspace",
            "META_MEMORY_SUBJECT_ID": "ambient-subject",
            "META_MEMORY_OUTBOX": str(self.root / "ambient-outbox"),
            "META_MEMORY_TIMEOUT": "99",
        }, clear=False):
            loaded = RemoteConfig.load(config_path)
            explicit = RemoteConfig.load(config_path, subject_id="explicit-child")
        self.assertEqual(loaded.url, "https://pinned.example.test")
        self.assertEqual(loaded.agent_id, "pinned-agent")
        self.assertEqual(loaded.workspace_id, "pinned-workspace")
        self.assertEqual(loaded.subject_id, "pinned-subject")
        self.assertEqual(loaded.outbox_dir, pinned_outbox.resolve())
        self.assertEqual(loaded.timeout_seconds, 11)
        self.assertEqual(explicit.subject_id, "explicit-child")

    def test_exact_text_and_crlf_file_contents_are_hashed_and_sent_unchanged(self) -> None:
        config_path = self.root / "exact.json"
        config_path.write_text(json.dumps({
            "url": "https://memory.example.test", "token_env": "TEST_META_MEMORY_TOKEN",
            "agent_id": "home-robot", "workspace_id": "household:home", "subject_id": "person:owner",
            "outbox_dir": str(self.root / "exact-outbox"),
        }), encoding="utf-8")
        query = "  exact question\r\n"
        answer = "  exact answer\r\n"
        query_file = self.root / "query-crlf.txt"
        answer_file = self.root / "answer-crlf.txt"
        query_file.write_bytes(query.encode("utf-8"))
        answer_file.write_bytes(answer.encode("utf-8"))
        sent: list[dict[str, object]] = []

        def request(_client, _method: str, path: str, payload=None, **_kwargs):
            body = dict(payload or {})
            sent.append(body)
            result = {"status": "ok", "turn_id": body.get("turn_id")}
            if not path.endswith("/before"):
                result["answer_sha256"] = body.get("answer_sha256")
            return result

        with patch.object(RemoteMemoryClient, "_request", request), patch("builtins.print"):
            self.assertEqual(main([
                "--config", str(config_path), "before", "--session-id", "exact-session",
                "--workspace-id", "household:away", "--subject-id", "person:child",
                "--turn-id", "exact-turn", "--query-file", str(query_file),
            ]), 0)
            self.assertEqual(main([
                "--config", str(config_path), "after", "--turn-id", "exact-turn",
                "--assistant-file", str(answer_file),
            ]), 0)
        self.assertEqual(sent[0]["query"], query)
        self.assertEqual(sent[1]["assistant_text"], answer)
        self.assertEqual(sent[1]["workspace_id"], "household:away")
        self.assertEqual(sent[1]["subject_id"], "person:child")
        self.assertEqual(sent[1]["session_id"], "exact-session")
        self.assertEqual(sent[1]["answer_sha256"], hashlib.sha256(answer.encode("utf-8")).hexdigest())

    def test_cli_offline_child_turn_replays_from_default_config_process(self) -> None:
        config_path = self.root / "cli-replay.json"
        config_path.write_text(json.dumps({
            "url": self.config.url, "token_env": self.config.token_env,
            "agent_id": self.config.agent_id, "workspace_id": self.config.workspace_id,
            "subject_id": self.config.subject_id, "audience_id": self.config.audience_id,
            "channel_id": self.config.channel_id, "outbox_dir": str(self.root / "cli-replay-outbox"),
        }), encoding="utf-8")
        answer_file = self.root / "offline-answer.txt"
        answer_file.write_text("Child-scoped offline answer", encoding="utf-8")
        with patch.object(RemoteMemoryClient, "_request", side_effect=RemoteTransportError("offline")), patch("builtins.print"):
            self.assertEqual(main([
                "--config", str(config_path), "before", "--turn-id", "cli-offline-child",
                "--session-id", "child-session", "--workspace-id", "household:away",
                "--subject-id", "person:child", "--query", "Child-scoped offline request",
            ]), 0)
            self.assertEqual(main([
                "--config", str(config_path), "after", "--turn-id", "cli-offline-child",
                "--assistant-file", str(answer_file),
            ]), 0)

        replayed: list[dict[str, object]] = []

        def recovered(_client, _method: str, _path: str, payload=None, **_kwargs):
            body = dict(payload or {})
            replayed.append(body)
            return {
                "status": "ok", "turn_id": body.get("turn_id"),
                "answer_sha256": body.get("answer_sha256", ""),
            }

        with patch.object(RemoteMemoryClient, "_request", recovered), patch("builtins.print"):
            self.assertEqual(main(["--config", str(config_path), "recovery"]), 0)
        self.assertEqual(len(replayed), 2)
        for body in replayed:
            self.assertEqual(body["workspace_id"], "household:away")
            self.assertEqual(body["subject_id"], "person:child")
            self.assertEqual(body["session_id"], "child-session")

    def test_redirect_is_refused_without_forwarding_authorization(self) -> None:
        seen: list[str] = []

        class Sink(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                seen.append(str(self.headers.get("Authorization") or ""))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def log_message(self, _format: str, *_args) -> None:
                return

        sink = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Sink)

        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{sink.server_address[1]}/sink")
                self.end_headers()

            def log_message(self, _format: str, *_args) -> None:
                return

        front = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (sink, front)]
        for thread in threads:
            thread.start()
        try:
            client = RemoteMemoryClient(RemoteConfig(
                url=f"http://127.0.0.1:{front.server_address[1]}",
                token_env="TEST_META_MEMORY_TOKEN", agent_id="home-robot",
                workspace_id="household:home", subject_id="person:owner",
                outbox_dir=self.root / "redirect-outbox",
            ))
            with self.assertRaisesRegex(RemoteSemanticError, "redirect refused"):
                client.status()
            self.assertEqual(seen, [])
        finally:
            for server in (front, sink):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=5)

    def test_outbox_rejects_replay_through_a_different_origin(self) -> None:
        first = RemoteMemoryClient(self.config)
        first._request = lambda *_args, **_kwargs: (_ for _ in ()).throw(RemoteTransportError("offline"))  # type: ignore[method-assign]
        first.before("private server A request", session_id="origin-session", turn_id="origin-turn")
        second = RemoteMemoryClient(RemoteConfig(
            url="https://server-b.example.test", token_env="TEST_META_MEMORY_TOKEN",
            agent_id=self.config.agent_id, workspace_id=self.config.workspace_id,
            subject_id=self.config.subject_id, audience_id=self.config.audience_id,
            channel_id=self.config.channel_id, outbox_dir=self.config.outbox_dir,
        ))
        calls: list[object] = []
        second._request = lambda *_args, **_kwargs: calls.append(object()) or {"status": "ok"}  # type: ignore[method-assign]
        result = second.replay()
        self.assertEqual(result["status"], "needs_action")
        self.assertEqual(result["pending"], 1)
        self.assertEqual(calls, [])

    def test_corrupt_pending_item_is_counted_and_reported_blocked(self) -> None:
        client = RemoteMemoryClient(self.config)
        client.outbox.pending.mkdir(parents=True)
        (client.outbox.pending / "50-corrupt.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(client.outbox.pending_count(), 1)
        result = client.replay()
        self.assertEqual(result["status"], "needs_action")
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["replayed"][0]["status"], "blocked")

    def test_complete_payload_is_queued_before_turn_receipt_write(self) -> None:
        client = RemoteMemoryClient(self.config)
        with patch.object(client.outbox, "save_state", side_effect=SystemExit("simulated crash")):
            with self.assertRaises(SystemExit):
                client.before("  preserved before crash\r\n", session_id="crash-session", turn_id="crash-turn")
        records = client.outbox.pending_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][1]["payload"]["query"], "  preserved before crash\r\n")

    def test_empty_success_response_keeps_before_in_local_outbox(self) -> None:
        client = RemoteMemoryClient(self.config)
        client._request = lambda *_args, **_kwargs: {}  # type: ignore[method-assign]
        result = client.before("needs explicit ack", session_id="ack-session", turn_id="ack-turn")
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(client.outbox.pending_count(), 1)

    def test_shared_world_write_is_durable_and_read_commands_are_public(self) -> None:
        client = RemoteMemoryClient(self.config)
        client._request = lambda *_args, **_kwargs: (_ for _ in ()).throw(RemoteTransportError("offline"))  # type: ignore[method-assign]
        queued = client.publish_activity({"summary": "Fridge failed", "occurred_at": "2026-07-22T00:00:00Z"})
        self.assertEqual(queued["status"], "local_outbox")
        self.assertEqual(client.outbox.pending_records()[0][1]["operation"], "activity")

        calls: list[tuple[str, dict[str, object]]] = []

        def read(_method: str, path: str, payload=None, *, query=None):
            calls.append((path, dict(query or {})))
            return {"status": "ok", "activities": [] if path.endswith("activities") else None,
                    "observations": [] if path.endswith("spatial-observations") else None}

        clean = RemoteMemoryClient(RemoteConfig(
            **{**self.config.__dict__, "outbox_dir": self.root / "read-outbox"}
        ))
        clean._request = read  # type: ignore[method-assign]
        clean.shared("feed", payload={"limit": 7})
        clean.spatial("search", payload={"query": "water sink", "limit": 3})
        self.assertEqual(calls[0][0], "/v1/activities")
        self.assertEqual(calls[0][1]["limit"], 7)
        self.assertEqual(calls[1][1]["query"], "water sink")

    def test_all_durable_shared_writes_replay_after_connectivity_returns(self) -> None:
        client = RemoteMemoryClient(self.config)
        client._request = lambda *_args, **_kwargs: (_ for _ in ()).throw(RemoteTransportError("offline"))  # type: ignore[method-assign]
        writes = [
            client.publish_activity({"summary": "Door opened", "occurred_at": "2026-07-22T00:00:00Z"}),
            client.publish_state({"state_key": "door.front", "summary": "open", "value": "open"}),
            client.observe({"content": "Package beside door", "observed_at": "2026-07-22T00:01:00Z"}),
            client.map("put", payload={"map_id": "home", "coordinate_frame": "map"}),
        ]
        self.assertTrue(all(item["status"] == "local_outbox" for item in writes))
        self.assertEqual(client.outbox.pending_count(), 4)

        acknowledged: list[str] = []

        def recovered(_method: str, path: str, payload=None, **_kwargs):
            acknowledged.append(path)
            result_key = {
                "/v1/activities": "activity",
                "/v1/states": "state",
                "/v1/spatial-observations": "observation",
                "/v1/maps": "map",
            }[path]
            return {"status": "ok", result_key: {"idempotency_key": (payload or {}).get("idempotency_key")}}

        client._request = recovered  # type: ignore[method-assign]
        result = client.replay()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pending"], 0)
        self.assertCountEqual(acknowledged, [
            "/v1/activities", "/v1/states", "/v1/spatial-observations", "/v1/maps",
        ])

    def test_asset_upload_restarts_once_when_server_lost_incomplete_upload(self) -> None:
        client = RemoteMemoryClient(self.config)
        source = self.root / "room-scan.bin"
        content = b"room-scan-data"
        source.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        binary_paths: list[str] = []

        def request(_method: str, path: str, payload=None, **_kwargs):
            if path == "/v1/assets/uploads":
                return {"status": "ok", "upload_id": "fresh-upload", "chunk_size": 65536}
            if path.endswith("/complete"):
                return {
                    "status": "ok",
                    "asset": {"asset_id": "asset-1", "sha256": digest, "byte_size": len(content)},
                }
            self.fail(f"unexpected request: {path}")

        def request_binary(_method: str, path: str, _data: bytes, **_kwargs):
            binary_paths.append(path)
            if "stale-upload" in path:
                raise RemoteSemanticError("upload not found", status_code=404)
            return {"status": "ok"}

        client._request = request  # type: ignore[method-assign]
        client._request_binary = request_binary  # type: ignore[method-assign]
        with patch.object(client.outbox, "load_upload", side_effect=[{
            "upload_id": "stale-upload", "chunk_size": 65536, "uploaded_parts": [],
        }, None]), patch.object(client.outbox, "clear_upload", wraps=client.outbox.clear_upload) as cleared:
            result = client.upload_asset(source)
        self.assertEqual(result["asset"]["asset_id"], "asset-1")
        cleared.assert_called_once()
        self.assertEqual(len(binary_paths), 2)
        self.assertIn("stale-upload", binary_paths[0])
        self.assertIn("fresh-upload", binary_paths[1])

    def test_installer_outputs_portable_non_secret_skill_and_launchers(self) -> None:
        result = install_remote_agent(
            "home-robot", self.root / "skills", "https://memory.example.test",
            "household:home", "person:owner", audience_id="family:home",
            channel_id="household-events", token_env="TEST_META_MEMORY_TOKEN",
            outbox_dir=self.root / "durable-outbox",
        )
        target = self.root / "skills" / "meta-memory-remote"
        self.assertEqual(result["status"], "needs_action")
        self.assertTrue((target / "SKILL.md").is_file())
        self.assertTrue((target / "meta-memory-remote").is_file())
        self.assertTrue((target / "meta-memory-remote.cmd").is_file())
        joined = "\n".join(path.read_text(encoding="utf-8") for path in target.iterdir() if path.is_file())
        self.assertNotIn(self.token, joined)
        self.assertIn("<launcher> before --turn-id", joined)
        self.assertIn("shared_context", joined)
        config = json.loads((target / "remote-config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["token_env"], "TEST_META_MEMORY_TOKEN")
        self.assertNotIn("session_id", config)
        self.assertEqual(config["audience_id"], "family:home")
        posix_bytes = (target / "meta-memory-remote").read_bytes()
        windows_bytes = (target / "meta-memory-remote.cmd").read_bytes()
        self.assertTrue(posix_bytes.startswith(b"#!/bin/sh\n"))
        self.assertNotIn(b"\r\n", posix_bytes)
        self.assertIn(b"\r\n", windows_bytes)
        self.assertNotIn(b"\r\r\n", windows_bytes)
        self.assertEqual(result["direct_argv"][0], str(Path(sys.executable).resolve()))

        completed = subprocess.run(
            [sys.executable, "-m", "meta_memory.remote_client", "--config", str(target / "remote-config.json"), "--help"],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_nested_remote_action_help_is_specific_and_actionable(self) -> None:
        cases = [
            (("shared", "feed"), ("--limit", "--channel-id"), ("--state-key", "--observation-id")),
            (("spatial", "search"), ("search_query", "--query", "--map-id"), ("--observation-id", "--location-id")),
            (("asset", "upload"), ("--file", "--media-type", "--metadata-file"), ("--asset-id", "--output", "--limit")),
            (("map", "put"), ("--payload-file",), ("--map-id", "--include-history", "--limit")),
        ]
        for command, expected, absent in cases:
            with self.subTest(command=command):
                completed = subprocess.run(
                    [sys.executable, "-m", "meta_memory.remote_client", *command, "--help"],
                    capture_output=True, text=True, encoding="utf-8", check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                for flag in expected:
                    self.assertIn(flag, completed.stdout)
                for flag in absent:
                    self.assertNotIn(flag, completed.stdout)

        required = subprocess.run(
            [sys.executable, "-m", "meta_memory.remote_client", "asset", "upload"],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(required.returncode, 2)
        self.assertIn("--file", required.stderr)

    def test_windows_launcher_escapes_percent_expansion_in_paths(self) -> None:
        result = install_remote_agent(
            "home-robot", self.root / "skills-%TEMP%", "https://memory.example.test",
            "household:home", "person:owner", token_env="TEST_META_MEMORY_TOKEN",
            outbox_dir=self.root / "outbox-%TEMP%",
        )
        command = Path(result["launchers"]["windows"]).read_bytes().decode("utf-8")
        self.assertIn("%%TEMP%%", command)
        self.assertNotIn("\r\r\n", command)

    def test_cli_reads_utf8_files_and_never_prints_token(self) -> None:
        request_file = self.root / "请求.txt"
        request_file.write_text("机器人看到了什么？", encoding="utf-8")
        config_file = self.root / "remote.json"
        config_file.write_text(json.dumps({
            "url": "https://memory.example.test", "token_env": "TEST_META_MEMORY_TOKEN",
            "agent_id": "home-robot", "workspace_id": "household:home", "subject_id": "person:owner",
            "outbox_dir": str(self.root / "cli-outbox"),
        }), encoding="utf-8")
        with patch.object(RemoteMemoryClient, "_request", side_effect=RemoteTransportError(self.token)):
            with patch("builtins.print") as output:
                code = main([
                    "--config", str(config_file), "before", "--session-id", "cli-session",
                    "--turn-id", "cli-turn", "--query-file", str(request_file),
                ])
        self.assertEqual(code, 0)
        rendered = str(output.call_args.args[0])
        self.assertIn('"status": "degraded"', rendered)
        self.assertNotIn(self.token, rendered)


if __name__ == "__main__":
    unittest.main()
