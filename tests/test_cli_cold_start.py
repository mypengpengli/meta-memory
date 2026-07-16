from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]


class ColdStartCliTests(unittest.TestCase):
    """Exercise the public CLI without a test process priming legacy imports."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _config_path(self, label: str) -> Path:
        root = self.root / label
        root.mkdir(parents=True, exist_ok=True)
        config = root / "config.toml"
        store = root / "store"
        config.write_text(
            "\n".join(
                [
                    "[user]",
                    'name = "Cold Start"',
                    'id = "cold-start"',
                    "",
                    "[storage]",
                    f"path = {json.dumps(str(store))}",
                    "",
                    "[maintenance]",
                    "enabled = false",
                    "",
                    "[dream]",
                    "enabled = false",
                    "heartbeat_enabled = false",
                    "deep_enabled = false",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return config

    def _environment(self, root: Path) -> dict[str, str]:
        environment = os.environ.copy()
        # The source checkout itself is importable through cwd.  Deliberately
        # do not expose its legacy ``scripts`` directory through PYTHONPATH:
        # every subprocess must rely on the public CLI bootstrap.
        environment.pop("PYTHONPATH", None)
        environment.pop("META_MEMORY_CONFIG", None)

        # ``schedule status`` is read-only, but a minimal Linux/macOS test
        # environment may not have its native scheduler binary installed.
        # Supply a harmless executable that reports "not installed" so this
        # test remains about CLI cold-start imports rather than host tooling.
        if os.name != "nt":
            shim_dir = root / "scheduler-shim"
            shim_dir.mkdir(parents=True, exist_ok=True)
            executable = shim_dir / ("launchctl" if sys.platform == "darwin" else "crontab")
            executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            executable.chmod(executable.stat().st_mode | 0o111)
            environment["PATH"] = str(shim_dir) + os.pathsep + environment.get("PATH", "")
        return environment

    def _run(self, entrypoint: str, command: tuple[str, ...], *, label: str) -> dict[str, object]:
        config = self._config_path(label)
        if entrypoint == "module":
            invocation = [sys.executable, "-m", "meta_memory.cli"]
        else:
            # This mirrors the installed setuptools console entry, which
            # imports ``main`` into a new interpreter and then invokes it.
            invocation = [sys.executable, "-c", "from meta_memory.cli import main; main()"]
        completed = subprocess.run(
            [*invocation, "--config", str(config), "--json", *command],
            cwd=SOURCE_ROOT,
            env=self._environment(config.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
        detail = f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        self.assertEqual(completed.returncode, 0, detail)
        self.assertNotIn("ModuleNotFoundError", completed.stdout + completed.stderr, detail)
        payload = json.loads(completed.stdout)
        self.assertIsInstance(payload, dict)
        self.assertIn("status", payload)
        return payload

    def test_critical_read_only_commands_bootstrap_in_fresh_processes(self) -> None:
        commands = (
            ("status",),
            ("doctor",),
            ("overview",),
            ("agent", "status", "--all", "--verbose"),
            ("agent", "upgrade-status", "--all"),
            ("schedule", "status"),
        )
        for entrypoint in ("module", "console"):
            for index, command in enumerate(commands):
                with self.subTest(entrypoint=entrypoint, command=command):
                    self._run(entrypoint, command, label=f"{entrypoint}-{index}")


if __name__ == "__main__":
    unittest.main()
