from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from meta_memory.agent_specs import custom_agent_spec
from meta_memory.config import AppConfig, load_config, save_config


class PortablePathTests(unittest.TestCase):
    def test_relative_store_in_existing_config_is_config_relative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config" / "config.toml"
            config_path.parent.mkdir()
            config_path.write_text('[storage]\npath = "relative-store"\n', encoding="utf-8")

            loaded = load_config(config_path)

            self.assertEqual(loaded.store, (config_path.parent / "relative-store").resolve())
            self.assertTrue(loaded.store.is_absolute())

    def test_save_config_canonicalizes_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(path=root / "config" / "config.toml", store=Path("relative-store"))

            save_config(config)

            self.assertEqual(config.store, (config.path.parent / "relative-store").resolve())
            self.assertEqual(load_config(config.path).store, config.store)

    def test_custom_agent_paths_are_pinned_at_install_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = Path.cwd()
            try:
                os.chdir(root)
                spec = custom_agent_spec(
                    "portable-agent",
                    Path("agent") / "skills",
                    Path("agent") / "AGENTS.md",
                )
            finally:
                os.chdir(previous)

            self.assertEqual(spec.skill_dir, (root / "agent" / "skills" / "meta-memory").resolve())
            self.assertEqual(spec.host_instruction_file, (root / "agent" / "AGENTS.md").resolve())


if __name__ == "__main__":
    unittest.main()
