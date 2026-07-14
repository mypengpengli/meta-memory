from __future__ import annotations

import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from meta_memory.backup import backup_app, backup_store, restore_app, restore_store
from meta_memory.config import AppConfig, load_config, save_config
from meta_memory.legacy import bootstrap

bootstrap()
from _common import ensure_store_ready, open_db


class PortableBackupRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = AppConfig(
            path=self.root / "source-config.toml",
            user_name="Ada",
            user_id="ada",
            store=self.root / "source-store",
        )
        save_config(self.config)
        ensure_store_ready(self.config.store)
        (self.config.store / "profile" / "portable.md").write_text("A portable memory.", encoding="utf-8")
        connection = open_db(self.config.store)
        connection.execute("CREATE TABLE IF NOT EXISTS backup_probe(value TEXT)")
        connection.execute("INSERT INTO backup_probe(value) VALUES('preserved')")
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _portable_archive(self) -> Path:
        archive = self.root / "portable.zip"
        backup_app(self.config, archive)
        return archive

    def test_portable_backup_includes_config_manifest_and_consistent_sqlite(self) -> None:
        archive = self._portable_archive()
        with zipfile.ZipFile(archive) as package:
            names = set(package.namelist())
            manifest = json.loads(package.read("manifest.json"))
        self.assertTrue({"manifest.json", "checksums.sha256", "config.toml", "store/db/memory_index.sqlite"}.issubset(names))
        self.assertFalse(any(name.endswith(("-wal", "-shm", "-journal")) for name in names))
        self.assertEqual(manifest["format_version"], 1)
        self.assertEqual(manifest["config_relative_path"], "config.toml")
        self.assertEqual(manifest["database_relative_path"], "store/db/memory_index.sqlite")
        self.assertTrue(manifest["database_sha256"])
        self.assertIn("016", manifest["schema_versions"])

        restored_config = AppConfig(path=self.root / "restored-config.toml", store=self.root / "placeholder")
        save_config(restored_config)
        restored_store = self.root / "restored-store"
        result = restore_app(restored_config, archive, restored_store)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["health"]["status"], "ok")
        self.assertTrue((restored_store / "profile" / "portable.md").is_file())
        self.assertEqual(load_config(restored_config.path).store.resolve(), restored_store.resolve())
        self.assertEqual(load_config(restored_config.path).user_id, "ada")
        connection = open_db(restored_store)
        self.assertEqual(connection.execute("SELECT value FROM backup_probe").fetchone()[0], "preserved")
        connection.close()

    def test_restore_rejects_tampering_and_zip_slip_before_touching_destination(self) -> None:
        archive = self._portable_archive()
        tampered = self.root / "tampered.zip"
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                payload = source.read(info.filename)
                if info.filename == "store/profile/portable.md":
                    payload = b"modified after backup"
                target.writestr(info, payload)
        destination = self.root / "must-stay-empty"
        with self.assertRaisesRegex(ValueError, "checksum"):
            restore_store(tampered, destination)
        self.assertFalse(destination.exists())

        malicious = self.root / "zip-slip.zip"
        with zipfile.ZipFile(malicious, "w") as package:
            package.writestr("../outside.txt", "not allowed")
        with self.assertRaisesRegex(ValueError, "unsafe|absolute"):
            restore_store(malicious, destination)
        self.assertFalse((self.root / "outside.txt").exists())

        linked = self.root / "symlink.zip"
        info = zipfile.ZipInfo("store/linked")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(linked, "w") as package:
            package.writestr(info, "outside")
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            restore_store(linked, destination)

    def test_force_is_required_for_an_existing_nonempty_store_and_legacy_api_remains_usable(self) -> None:
        archive = self._portable_archive()
        destination = self.root / "existing-store"
        destination.mkdir()
        (destination / "do-not-delete-without-force.txt").write_text("sentinel", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not empty"):
            restore_store(archive, destination)
        self.assertTrue((destination / "do-not-delete-without-force.txt").exists())
        result = restore_store(archive, destination, force=True)
        self.assertEqual(result["status"], "ok")
        self.assertFalse((destination / "do-not-delete-without-force.txt").exists())
        self.assertTrue((destination / "db" / "memory_index.sqlite").is_file())

        store_only = self.root / "store-only.zip"
        backup_store(self.config.store, store_only)
        legacy_destination = self.root / "legacy-store"
        legacy = restore_store(store_only, legacy_destination)
        self.assertEqual(legacy["status"], "ok")
        self.assertTrue((legacy_destination / "db" / "memory_index.sqlite").is_file())

        # Archives created by the pre-portable ``backup_store`` contained only
        # ``store/`` and no manifest.  They remain readable during upgrades.
        old_style = self.root / "pre-portable.zip"
        with zipfile.ZipFile(old_style, "w", compression=zipfile.ZIP_DEFLATED) as package:
            for path in self.config.store.rglob("*"):
                if path.is_file() and not path.name.endswith(("-wal", "-shm", "-journal")):
                    package.write(path, Path("store") / path.relative_to(self.config.store))
        old_destination = self.root / "pre-portable-store"
        old_result = restore_store(old_style, old_destination)
        self.assertEqual(old_result["status"], "ok")
        self.assertIn("Legacy store-only", old_result["warnings"][0])
        self.assertTrue((old_destination / "db" / "memory_index.sqlite").is_file())


if __name__ == "__main__":
    unittest.main()
