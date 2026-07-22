from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import db_migrations


LEGACY_022_CHECKSUM = "bf5b3f1442843d42bbfd8818c1cbfc23d9c7c087bbead9f88f17961fc653ff93"


class MigrationCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "memory.sqlite"
        self.connections: list[sqlite3.Connection] = []

    def tearDown(self) -> None:
        for connection in self.connections:
            connection.close()
        self.temporary.cleanup()

    def _legacy_022_database(self) -> sqlite3.Connection:
        files = db_migrations.migration_files()
        through_021 = [path for path in files if path.name.split("_", 1)[0] <= "021"]
        canonical_022 = next(path for path in files if path.name.startswith("022_"))
        lines = canonical_022.read_text(encoding="utf-8").splitlines(keepends=True)
        # This is the exact predecessor represented by LEGACY_022_CHECKSUM:
        # it lacked only the last_run_uid column and its index.
        predecessor = "".join(lines[:27] + lines[31:])
        self.assertEqual(
            db_migrations.hashlib.sha256(predecessor.encode("utf-8")).hexdigest(),
            LEGACY_022_CHECKSUM,
        )
        connection = sqlite3.connect(self.db)
        self.connections.append(connection)
        with patch.object(db_migrations, "migration_files", return_value=through_021):
            db_migrations.run_migrations(connection)
        connection.executescript(predecessor)
        connection.execute(
            "INSERT INTO schema_migrations(version,checksum) VALUES('022',?)",
            (LEGACY_022_CHECKSUM,),
        )
        connection.commit()
        return connection

    def test_known_022_predecessor_is_completed_and_canonicalized(self) -> None:
        connection = self._legacy_022_database()
        result = db_migrations.run_migrations(connection)
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(dream_nodes)")
        }
        indexes = {
            str(row[1]) for row in connection.execute("PRAGMA index_list(dream_nodes)")
        }
        recorded = str(
            connection.execute(
                "SELECT checksum FROM schema_migrations WHERE version='022'"
            ).fetchone()[0]
        )
        canonical = db_migrations.checksum(
            next(path for path in db_migrations.migration_files() if path.name.startswith("022_"))
        )
        versions = {
            str(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")
        }

        self.assertIn("last_run_uid", columns)
        self.assertIn("idx_dream_nodes_last_run", indexes)
        self.assertEqual(recorded, canonical)
        self.assertIn("024", versions)
        self.assertIn("024", result["applied"])
        self.assertIn("026", versions)
        self.assertIn("026", result["applied"])

    def test_unknown_checksum_still_fails_closed(self) -> None:
        connection = self._legacy_022_database()
        connection.execute(
            "UPDATE schema_migrations SET checksum='unknown' WHERE version='022'"
        )
        connection.commit()
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            db_migrations.run_migrations(connection)


if __name__ == "__main__":
    unittest.main()
