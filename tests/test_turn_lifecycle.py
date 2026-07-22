from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from meta_memory.config import AppConfig
from meta_memory.legacy import bootstrap
from meta_memory.runtime import after, before, read_text

bootstrap()
from _common import open_db


class TurnLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = AppConfig(path=root / "config.toml", user_name="Ada", user_id="ada", store=root / "store")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _count(self, role: str) -> int:
        conn = open_db(self.config.store)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM raw_events WHERE message_role=?", (role,)).fetchone()[0])
        finally:
            conn.close()

    def test_begin_is_durable_and_idempotent_per_turn(self) -> None:
        first = before(
            self.config,
            query="Please retain this request even if the host stops.",
            session="session-a",
            start=self.temp.name,
            agent_id="codex",
            turn_uid="turn-a",
        )
        self.assertEqual(first["status"], "ok")
        self.assertEqual(self._count("user"), 1)

        retry = before(
            self.config,
            query="Please retain this request even if the host stops.",
            session="session-a",
            start=self.temp.name,
            agent_id="codex",
            turn_uid="turn-a",
        )
        self.assertTrue(retry["idempotent"])
        self.assertEqual(self._count("user"), 1)

        other_turn = before(
            self.config,
            query="Please retain this request even if the host stops.",
            session="session-a",
            start=self.temp.name,
            agent_id="codex",
            turn_uid="turn-b",
        )
        self.assertFalse(other_turn["idempotent"])
        self.assertEqual(self._count("user"), 2)

        conn = open_db(self.config.store)
        try:
            row = conn.execute("SELECT status,user_event_id FROM turns WHERE turn_uid='turn-a'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "started")
        self.assertTrue(row[1])

    def test_completion_is_idempotent_and_rejects_different_reply(self) -> None:
        started = before(
            self.config,
            query="What changed?",
            session="session-b",
            start=self.temp.name,
            agent_id="codex",
            turn_uid="turn-complete",
        )
        finished = after(self.config, turn_uid=started["turn_id"], assistant_text="The migration was applied.")
        retried = after(self.config, turn_uid=started["turn_id"], assistant_text="The migration was applied.")
        self.assertFalse(finished["idempotent"])
        self.assertTrue(retried["idempotent"])
        self.assertEqual(finished["assistant_event_id"], retried["assistant_event_id"])
        self.assertEqual(self._count("assistant"), 1)
        with self.assertRaisesRegex(ValueError, "different assistant response"):
            after(self.config, turn_uid=started["turn_id"], assistant_text="A different reply.")

    def test_retrieval_failure_keeps_user_event(self) -> None:
        with patch("memory_runtime.prepare_context", side_effect=RuntimeError("offline retrieval")):
            result = before(
                self.config,
                query="This request must survive a retrieval failure.",
                session="session-c",
                start=self.temp.name,
                agent_id="codex",
                turn_uid="turn-degraded",
            )
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(self._count("user"), 1)
        conn = open_db(self.config.store)
        try:
            row = conn.execute("SELECT context_status,last_error FROM turns WHERE turn_uid='turn-degraded'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "degraded")
        self.assertIn("offline retrieval", row[1])

    def test_answer_file_preserves_exact_whitespace_and_crlf(self) -> None:
        answer_file = Path(self.temp.name) / "answer.txt"
        answer_file.write_bytes(b"\xef\xbb\xbf  exact answer\r\nsecond line  \r\n")
        exact = read_text(path=answer_file, preserve=True)
        self.assertEqual(exact, "  exact answer\r\nsecond line  \r\n")
        started = before(
            self.config,
            query="Preserve the exact answer bytes after UTF-8 decoding.",
            session="session-exact",
            start=self.temp.name,
            agent_id="codex",
            turn_uid="turn-exact-answer",
        )
        completed = after(self.config, turn_uid=str(started["turn_id"]), assistant_text=exact)
        self.assertEqual(completed["answer_sha256"], hashlib.sha256(exact.encode("utf-8")).hexdigest())
        conn = open_db(self.config.store)
        try:
            stored = conn.execute(
                "SELECT content FROM raw_events WHERE id=?",
                (int(completed["assistant_event_id"]),),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(stored, exact)


if __name__ == "__main__":
    unittest.main()
