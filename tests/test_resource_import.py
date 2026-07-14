from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from meta_memory.config import AppConfig
from meta_memory.importer import import_file
from meta_memory.legacy import bootstrap
from meta_memory.runtime import before, search

bootstrap()
from _common import open_db
from assemble_context import assemble_context
from background_review import run_pending


class ResourceImportRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = AppConfig(
            path=root / "config.toml",
            user_name="Ada",
            user_id="ada",
            store=root / "store",
        )
        self.source = root / "reference.md"
        # Put the token beyond the raw-event audit preview.  A match proves
        # search examines stored chunks rather than only the import preview.
        self.token = "needle-resource-omega"
        self.resource_only_text = "resource-body-must-not-inject"
        self.preview_only_text = "resource-preview-must-not-inject"
        self.source.write_text(
            (f"{self.preview_only_text}\n" + "ordinary reference material\n" * 900)
            + f"\nFinal evidence: {self.token}; {self.resource_only_text}\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_resources_are_chunk_searchable_but_never_prompt_context(self) -> None:
        imported = import_file(
            self.config,
            file_path=self.source,
            project_name="demo",
            start=self.temp.name,
            agent_id="codex",
        )
        self.assertEqual(imported["status"], "ok")
        self.assertGreater(imported["chunks"], 1)
        self.assertEqual(imported["source"]["name"], "reference.md")
        self.assertTrue(imported["review"]["job_id"])

        conn = open_db(self.config.store)
        try:
            resource_count = int(conn.execute("SELECT COUNT(*) FROM resource_imports").fetchone()[0])
            chunk_count = int(conn.execute("SELECT COUNT(*) FROM resource_chunks").fetchone()[0])
            fact_count = int(conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(resource_count, 1)
        self.assertEqual(chunk_count, int(imported["chunks"]))
        self.assertEqual(fact_count, 0)

        # Automatic maintenance may retain a resource-only candidate, but it
        # must never promote document text into a normal factual Claim or
        # prompt context without a user-confirmed path.
        reviewed = run_pending(self.config.store, max_jobs=1, memory_mode="automatic")
        self.assertTrue(reviewed["results"][0]["unit_ids"])
        conn = open_db(self.config.store)
        try:
            rows = conn.execute(
                "SELECT memory_kind,verification_state,prompt_eligible,status FROM claims"
            ).fetchall()
        finally:
            conn.close()
        self.assertTrue(rows)
        self.assertTrue(all(tuple(row) == ("candidate", "resource", 0, "candidate") for row in rows))

        searched = search(self.config, query=self.token, project_name="demo", start=self.temp.name)
        resource = next(item for item in searched["results"] if item["memory_kind"] == "resource")
        self.assertEqual(resource["verification_state"], "resource")
        self.assertFalse(resource["prompt_eligible"])
        self.assertEqual(resource["resource"]["source_name"], "reference.md")
        self.assertIn(self.token, resource["summary"])
        self.assertIn(self.resource_only_text, resource["summary"])
        self.assertLessEqual(len(resource["summary"]), 722)

        # Even a source/evidence-style question must not turn the imported raw
        # preview or its indexed chunk into automatic prompt context.
        turn = before(
            self.config,
            query=f"What source evidence mentions {self.preview_only_text}?",
            session="resource-context-test",
            project_name="demo",
            start=self.temp.name,
            agent_id="codex",
            turn_uid="resource-context-turn",
        )
        self.assertEqual(turn["status"], "ok")
        self.assertNotIn(self.resource_only_text, turn["context"])
        self.assertNotIn(self.preview_only_text, turn["context"])
        self.assertTrue(turn["query_route"]["needs_session_search"])
        self.assertNotIn(self.resource_only_text, assemble_context({"selected": [resource]}))


if __name__ == "__main__":
    unittest.main()
