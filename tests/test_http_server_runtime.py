from __future__ import annotations

import http.client
import io
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from meta_memory.config import AppConfig
from meta_memory.http_api import APIServer, Principal


class HTTPServerRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.logs = io.StringIO()
        config = AppConfig(
            path=self.root / "config.toml",
            user_name="Hosted owner",
            user_id="server-default",
            store=self.root,
            maintenance_enabled=False,
            dream_enabled=False,
            dream_heartbeat_enabled=False,
            dream_deep_enabled=False,
        )
        principal = Principal(
            profile_id="profile-hosted",
            agent_id="remote-agent",
            workspaces=frozenset({"workspace-hosted"}),
            permissions=frozenset({"read"}),
        )
        self.server = APIServer(
            ("127.0.0.1", 0),
            self.root,
            {"never-log-this-token": principal},
            config=config,
            access_log=True,
            log_stream=self.logs,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.assertTrue(self.server._serving.wait(2))

    def tearDown(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.server_close()
        self.temp.cleanup()

    def request(
        self,
        path: str,
        *,
        request_id: str = "",
    ) -> tuple[int, dict[str, object], str]:
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        headers = {"Authorization": "Bearer never-log-this-token"}
        if request_id:
            headers["X-Request-ID"] = request_id
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        response_request_id = str(response.getheader("X-Request-ID") or "")
        status = response.status
        connection.close()
        self.server.wait_for_requests(2)
        return status, body, response_request_id

    def log_records(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.logs.getvalue().splitlines() if line.strip()]

    def test_ready_checks_bindings_schema_and_writable_volumes_without_probe_files(self) -> None:
        status, body, request_id = self.request("/readyz", request_id="proxy-request-123")

        self.assertEqual(status, 200, body)
        self.assertEqual(request_id, "proxy-request-123")
        self.assertEqual(body["status"], "ready")
        checks = body["checks"]
        self.assertEqual(checks["agent_bindings"]["count"], 1)
        self.assertTrue(checks["agent_bindings"]["loaded"])
        self.assertTrue(checks["database"]["accessible"])
        self.assertTrue(checks["database"]["schema_current"])
        self.assertTrue(checks["database"]["schema_version"])
        self.assertTrue(checks["store"]["writable"])
        self.assertTrue(checks["store"]["database_directory_writable"])
        self.assertTrue(checks["assets"]["writable"])
        self.assertTrue(checks["assets"]["objects_writable"])
        self.assertTrue(checks["assets"]["uploads_writable"])
        self.assertEqual(list(self.root.rglob(".meta-memory-ready-*")), [])

    def test_request_id_and_structured_access_log_exclude_headers_query_and_body(self) -> None:
        status, body, request_id = self.request(
            "/healthz?token=query-secret&note=private-body-text",
            request_id="edge.correlation:456",
        )

        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})
        self.assertEqual(request_id, "edge.correlation:456")
        records = self.log_records()
        request = next(row for row in records if row.get("event") == "http_request")
        self.assertEqual(request["request_id"], request_id)
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["path"], "/healthz")
        self.assertEqual(request["status"], 200)
        self.assertIn("duration_ms", request)
        encoded = self.logs.getvalue()
        self.assertNotIn("never-log-this-token", encoded)
        self.assertNotIn("query-secret", encoded)
        self.assertNotIn("private-body-text", encoded)
        self.assertNotIn("Authorization", encoded)

        _, _, generated = self.request("/healthz", request_id="invalid request id")
        self.assertNotEqual(generated, "invalid request id")
        self.assertRegex(generated, re.compile(r"^[0-9a-f]{32}$"))

        _, _, token_as_id = self.request(
            "/never-log-this-token", request_id="never-log-this-token"
        )
        self.assertNotEqual(token_as_id, "never-log-this-token")
        self.assertNotIn("never-log-this-token", self.logs.getvalue())

    def test_ready_returns_503_when_loaded_agent_bindings_are_missing(self) -> None:
        self.server.tokens.clear()

        status, body, _ = self.request("/readyz")

        self.assertEqual(status, 503, body)
        self.assertEqual(body["status"], "not_ready")
        self.assertEqual(body["checks"]["agent_bindings"]["status"], "error")
        self.assertEqual(body["checks"]["database"]["status"], "ok")

    def test_ready_returns_sanitized_503_when_database_is_unavailable(self) -> None:
        with patch(
            "meta_memory.http_api.open_db",
            side_effect=OSError("sensitive-volume-name"),
        ):
            status, body, _ = self.request("/readyz")

        self.assertEqual(status, 503, body)
        self.assertEqual(body["status"], "not_ready")
        database = body["checks"]["database"]
        self.assertEqual(database["status"], "error")
        self.assertEqual(database["error_type"], "OSError")
        self.assertNotIn("sensitive-volume-name", json.dumps(body))

    def test_shutdown_request_stops_serve_loop_without_same_thread_deadlock(self) -> None:
        requested = self.server.request_shutdown(reason="signal:SIGTERM")

        self.assertTrue(requested)
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(self.server.request_shutdown(reason="duplicate"))
        self.assertEqual(self.server._shutdown_reason, "signal:SIGTERM")
        events = [row.get("event") for row in self.log_records()]
        self.assertIn("server_shutdown_requested", events)


if __name__ == "__main__":
    unittest.main()
