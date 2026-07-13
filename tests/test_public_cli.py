from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from meta_memory.backup import backup_store, restore_store
from meta_memory.config import AppConfig, load_config, save_config
from meta_memory.dream import run_dream
from meta_memory.maintenance import maintain
from meta_memory.project_detection import bind_project, resolve_project
from meta_memory.runtime import after, before, search


class PublicCliRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = AppConfig(path=root / "config.toml", user_name="Ada", user_id="ada", store=root / "store")
        save_config(self.config)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_config_project_turn_maintenance_and_dream_are_shared(self) -> None:
        self.assertEqual(load_config(self.config.path).subject_id, "person:ada")
        project = bind_project(self.config, "demo", self.temp.name)
        save_config(self.config)
        self.assertEqual(resolve_project(self.config, "auto", self.temp.name).workspace_id, project.workspace_id)

        context = before(self.config, query="项目现在使用 SQLite。", session="turn-1", project_name="auto", start=self.temp.name)
        self.assertIn("hot_context", context)
        recorded = after(self.config, user_text="项目现在使用 SQLite。", assistant_text="已记录。", session="turn-1", project_name="auto", start=self.temp.name)
        self.assertTrue(recorded["user_event"]["inserted"])
        self.assertTrue(recorded["assistant_event"]["inserted"])

        maintenance = maintain(self.config, max_jobs=5)
        self.assertEqual(maintenance["status"], "ok")
        recalled = search(self.config, query="SQLite", project_name="auto", start=self.temp.name)
        self.assertEqual(len(recalled["results"]), 1)

        dream = run_dream(self.config)
        self.assertTrue(dream["inferred"])
        self.assertTrue(Path(str(dream["report"])).is_file())

    def test_backup_restores_a_consistent_store(self) -> None:
        after(self.config, user_text="我喜欢简短回答。", assistant_text="好的。", session="turn-2", project_name="demo", start=self.temp.name)
        maintain(self.config, max_jobs=5)
        archive = Path(self.temp.name) / "backup.zip"
        backup_store(self.config.store, archive)
        restored = Path(self.temp.name) / "restored"
        result = restore_store(archive, restored)
        self.assertEqual(result["status"], "ok")
        self.assertTrue((restored / "db" / "memory_index.sqlite").is_file())


if __name__ == "__main__":
    unittest.main()
