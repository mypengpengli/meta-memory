#!/usr/bin/env python3
"""Transactional, checksummed SQLite migrations for Meta Memory."""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path


def migrations_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "migrations"


def migration_files() -> list[Path]:
    return sorted(migrations_dir().glob("[0-9][0-9][0-9]_*.sql"))


def expected_schema_version() -> str:
    """Return the newest packaged migration without reading every script.

    ``open_db`` is called by short-lived CLI processes on every turn.  The
    former implementation recalculated every migration checksum and repaired
    FTS on each new process.  A single version lookup is enough for the normal
    path; the full checksum walk remains available whenever a migration is
    actually needed or an operator explicitly invokes this module.
    """

    files = migration_files()
    return files[-1].name.split("_", 1)[0] if files else ""


def schema_is_current(conn: sqlite3.Connection) -> bool:
    """Cheap, non-mutating schema sentinel for ordinary database opens."""

    expected = expected_schema_version()
    if not expected:
        return True
    try:
        row = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=? LIMIT 1", (expected,)
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row)


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# A pre-release 022 migration was applied on this project's original store
# before the final three ``dream_nodes.last_run_uid`` lines were added.  The
# exact predecessor hash is known and its only schema delta is repaired below.
# Keeping this narrow allow-list preserves checksum protection for every other
# mismatch while letting that real store upgrade normally.
KNOWN_PREDECESSOR_CHECKSUMS = {
    "022": frozenset(
        {
            "bf5b3f1442843d42bbfd8818c1cbfc23d9c7c087bbead9f88f17961fc653ff93",
        }
    ),
}


def repair_known_predecessor(
    conn: sqlite3.Connection,
    *,
    version: str,
    recorded_checksum: str,
    canonical_checksum: str,
) -> bool:
    """Complete one explicitly-known predecessor and canonicalize its row."""

    if recorded_checksum not in KNOWN_PREDECESSOR_CHECKSUMS.get(version, frozenset()):
        return False
    if version != "022":
        return False

    # Re-read after taking the write lock so two simultaneous upgrades remain
    # idempotent.  All repair statements and the checksum replacement commit
    # together; a crash cannot leave a canonical checksum on a partial schema.
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return False
        current = str(row[0])
        if current == canonical_checksum:
            conn.commit()
            return True
        if current not in KNOWN_PREDECESSOR_CHECKSUMS[version]:
            conn.rollback()
            return False
        ensure_columns(conn, "dream_nodes", {"last_run_uid": "TEXT"})
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dream_nodes_last_run "
            "ON dream_nodes(profile_id, last_run_uid, updated_at)"
        )
        conn.execute(
            "UPDATE schema_migrations SET checksum=? WHERE version=?",
            (canonical_checksum, version),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


LEGACY_COLUMNS = {
    "documents": {
        "title": "TEXT", "subject_id": "TEXT", "subject_name": "TEXT", "memory_kind": "TEXT", "page_role": "TEXT",
        "canonical": "INTEGER DEFAULT 0", "domain": "TEXT", "topic": "TEXT", "tags": "TEXT", "summary": "TEXT",
        "confidence": "REAL", "importance": "REAL DEFAULT 0.5", "status": "TEXT", "source": "TEXT",
        "start_at": "TEXT", "end_at": "TEXT", "related_people": "TEXT", "related_events": "TEXT",
        "related_topics": "TEXT", "related_sources": "TEXT", "supersedes": "TEXT", "replaced_by": "TEXT", "mtime": "REAL",
        "memory_id": "TEXT", "schema_version": "INTEGER DEFAULT 1", "content_hash": "TEXT",
    },
    "scores": {"hit_count": "INTEGER DEFAULT 0", "confidence": "REAL DEFAULT 0.0", "rank_score": "REAL DEFAULT 0.0", "last_hit_at": "TEXT"},
    "raw_events": {
        "subject_id": "TEXT", "subject_name": "TEXT", "session_id": "TEXT", "source_type": "TEXT", "source_ref": "TEXT",
        "content": "TEXT", "content_hash": "TEXT", "topic_hint": "TEXT", "domain_hint": "TEXT", "event_time": "TEXT",
        "created_at": "TEXT", "processed_state": "TEXT DEFAULT 'pending'", "processed_at": "TEXT",
        "batch_id": "TEXT", "classifier_kind": "TEXT", "classifier_domain": "TEXT", "target_memory_kind": "TEXT",
        "target_memory_path": "TEXT", "note": "TEXT",
        "session_card_id": "INTEGER",
        "sessionized_at": "TEXT",
        "source_trust": "REAL DEFAULT 1.0",
    },
    "maintenance_cursor": {"last_processed_event_id": "INTEGER DEFAULT 0", "last_organized_at": "TEXT", "last_heartbeat_at": "TEXT"},
    "memory_sources": {"memory_path": "TEXT", "raw_event_id": "INTEGER", "link_role": "TEXT", "created_at": "TEXT"},
}


def ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, sql_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def missing_column_sql(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> list[str]:
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    return [f"ALTER TABLE {table} ADD COLUMN {name} {sql_type};" for name, sql_type in columns.items() if name not in existing]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=? LIMIT 1",
            (name,),
        ).fetchone()
    )


def _record_fts_state(conn: sqlite3.Connection, name: str, count: int) -> None:
    if not _table_exists(conn, "fts_runtime_state"):
        return
    conn.execute(
        """
        INSERT INTO fts_runtime_state(index_name,source_count,refreshed_at)
        VALUES(?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(index_name) DO UPDATE SET
            source_count=excluded.source_count, refreshed_at=excluded.refreshed_at
        """,
        (name, int(count)),
    )


def _fts_state_exists(conn: sqlite3.Connection, name: str) -> bool:
    if not _table_exists(conn, "fts_runtime_state"):
        return False
    return bool(
        conn.execute(
            "SELECT 1 FROM fts_runtime_state WHERE index_name=? LIMIT 1", (name,)
        ).fetchone()
    )


def _create_resource_fts_triggers(conn: sqlite3.Connection) -> None:
    """Keep imported source chunks searchable without importer-side work."""

    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS resource_chunks_fts_ai
        AFTER INSERT ON resource_chunks BEGIN
            INSERT INTO resource_chunks_fts(rowid,chunk_uid,resource_uid,content)
            VALUES(new.rowid,new.chunk_uid,new.resource_uid,new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS resource_chunks_fts_au
        AFTER UPDATE OF chunk_uid,resource_uid,content ON resource_chunks BEGIN
            DELETE FROM resource_chunks_fts WHERE rowid=old.rowid;
            INSERT INTO resource_chunks_fts(rowid,chunk_uid,resource_uid,content)
            VALUES(new.rowid,new.chunk_uid,new.resource_uid,new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS resource_chunks_fts_ad
        AFTER DELETE ON resource_chunks BEGIN
            DELETE FROM resource_chunks_fts WHERE rowid=old.rowid;
        END;
        """
    )


def _create_session_card_fts_triggers(conn: sqlite3.Connection) -> None:
    """Keep compact cross-Agent session summaries FTS-searchable."""

    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS session_cards_fts_ai
        AFTER INSERT ON session_cards BEGIN
            INSERT INTO session_cards_fts(
                rowid,card_id,profile_id,workspace_id,subject_id,origin_agent_id,
                summary,tool_summary,open_questions
            ) VALUES(
                new.id,new.id,new.profile_id,new.workspace_id,new.subject_id,
                COALESCE(new.origin_agent_id,''),new.summary,new.tool_summary,
                new.open_questions
            );
        END;
        CREATE TRIGGER IF NOT EXISTS session_cards_fts_au
        AFTER UPDATE OF profile_id,workspace_id,subject_id,origin_agent_id,summary,tool_summary,open_questions
        ON session_cards BEGIN
            DELETE FROM session_cards_fts WHERE rowid=old.id;
            INSERT INTO session_cards_fts(
                rowid,card_id,profile_id,workspace_id,subject_id,origin_agent_id,
                summary,tool_summary,open_questions
            ) VALUES(
                new.id,new.id,new.profile_id,new.workspace_id,new.subject_id,
                COALESCE(new.origin_agent_id,''),new.summary,new.tool_summary,
                new.open_questions
            );
        END;
        CREATE TRIGGER IF NOT EXISTS session_cards_fts_ad
        AFTER DELETE ON session_cards BEGIN
            DELETE FROM session_cards_fts WHERE rowid=old.id;
        END;
        """
    )


def ensure_optional_fts(conn: sqlite3.Connection, *, refresh: bool = False) -> bool:
    """Create optional FTS indexes and rebuild them only when required.

    A missing FTS5 extension is still non-fatal.  ``refresh`` is used when a
    schema migration has just run (or by the explicit repair command); normal
    application opens only create missing indexes and never perform whole-table
    counts/rebuilds.
    """
    try:
        raw_exists = _table_exists(conn, "raw_events_fts")
        resource_exists = _table_exists(conn, "resource_chunks_fts")
        cards_exists = _table_exists(conn, "session_cards_fts")
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
                chunk_id UNINDEXED, doc_path UNINDEXED, heading, content
            )
            """
        )
        # Keep this independent from session_messages so a build of SQLite
        # without FTS5 degrades to deterministic LIKE search instead of
        # failing the whole store migration.
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS session_messages_fts USING fts5(
                content, tool_name
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS raw_events_fts USING fts5(
                raw_event_id UNINDEXED, content, topic_hint, domain_hint
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS resource_chunks_fts USING fts5(
                chunk_uid UNINDEXED, resource_uid UNINDEXED, content
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS session_cards_fts USING fts5(
                card_id UNINDEXED, profile_id UNINDEXED, workspace_id UNINDEXED,
                subject_id UNINDEXED, origin_agent_id UNINDEXED, summary,
                tool_summary, open_questions
            )
            """
        )
        _create_resource_fts_triggers(conn)
        _create_session_card_fts_triggers(conn)

        # Populate a newly-created index once.  The runtime state replaces the
        # old raw/source COUNT(*) checks on every one-shot command.
        if refresh or not raw_exists or not _fts_state_exists(conn, "raw_events"):
            conn.execute("DELETE FROM raw_events_fts")
            conn.execute(
                "INSERT INTO raw_events_fts(raw_event_id,content,topic_hint,domain_hint) "
                "SELECT id,content,topic_hint,domain_hint FROM raw_events"
            )
            _record_fts_state(
                conn, "raw_events", int(conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0])
            )
        if refresh or not resource_exists or not _fts_state_exists(conn, "resource_chunks"):
            conn.execute("DELETE FROM resource_chunks_fts")
            conn.execute(
                "INSERT INTO resource_chunks_fts(rowid,chunk_uid,resource_uid,content) "
                "SELECT rowid,chunk_uid,resource_uid,content FROM resource_chunks"
            )
            _record_fts_state(
                conn,
                "resource_chunks",
                int(conn.execute("SELECT COUNT(*) FROM resource_chunks").fetchone()[0]),
            )
        if refresh or not cards_exists or not _fts_state_exists(conn, "session_cards"):
            conn.execute("DELETE FROM session_cards_fts")
            conn.execute(
                """
                INSERT INTO session_cards_fts(
                    rowid,card_id,profile_id,workspace_id,subject_id,origin_agent_id,
                    summary,tool_summary,open_questions
                )
                SELECT id,id,profile_id,workspace_id,subject_id,
                       COALESCE(origin_agent_id,''),summary,tool_summary,open_questions
                FROM session_cards
                """
            )
            _record_fts_state(
                conn,
                "session_cards",
                int(conn.execute("SELECT COUNT(*) FROM session_cards").fetchone()[0]),
            )
        return True
    except sqlite3.OperationalError:
        return False


def ensure_legacy_indexes(conn: sqlite3.Connection) -> None:
    statements = [
        "CREATE INDEX IF NOT EXISTS idx_raw_events_subject_state ON raw_events(subject_id, processed_state, id)",
        "CREATE INDEX IF NOT EXISTS idx_raw_events_subject_hash ON raw_events(subject_id, content_hash)",
        "CREATE INDEX IF NOT EXISTS idx_raw_events_created_at ON raw_events(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_raw_events_subject_event_time ON raw_events(subject_id, event_time, id)",
        "CREATE INDEX IF NOT EXISTS idx_raw_events_topic_domain ON raw_events(topic_hint, domain_hint)",
        "CREATE INDEX IF NOT EXISTS idx_memory_sources_raw_event ON memory_sources(raw_event_id)",
        "CREATE INDEX IF NOT EXISTS idx_memory_sources_memory_path ON memory_sources(memory_path)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_sources_unique_link ON memory_sources(memory_path, raw_event_id, link_role)",
    ]
    for statement in statements:
        conn.execute(statement)


def run_migrations(conn: sqlite3.Connection, *, repair_fts: bool = False) -> dict[str, object]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied = {str(version): str(digest) for version, digest in conn.execute("SELECT version, checksum FROM schema_migrations")}
    completed: list[str] = []
    for path in migration_files():
        version = path.name.split("_", 1)[0]
        digest = checksum(path)
        if version in applied:
            if applied[version] != digest:
                repaired = repair_known_predecessor(
                    conn,
                    version=version,
                    recorded_checksum=applied[version],
                    canonical_checksum=digest,
                )
                if not repaired:
                    raise RuntimeError(f"Migration checksum mismatch for {path.name}; do not edit an applied migration.")
                applied[version] = digest
            continue
        script = path.read_text(encoding="utf-8")
        try:
            # executescript commits any pending transaction.  Put the migration,
            # legacy bridge, and checksum record in one explicit SQL transaction.
            bridge = []
            if version >= "002":
                for table, columns in LEGACY_COLUMNS.items():
                    bridge.extend(missing_column_sql(conn, table, columns))
            escaped_version = version.replace("'", "''")
            escaped_digest = digest.replace("'", "''")
            conn.executescript(
                "BEGIN IMMEDIATE;\n"
                + "\n".join(bridge)
                + "\n"
                + script
                + f"\nINSERT INTO schema_migrations(version, checksum) VALUES('{escaped_version}', '{escaped_digest}');\nCOMMIT;"
            )
        except Exception:
            conn.rollback()
            # Another process may have acquired the SQLite migration lock
            # after this connection took its initial ``applied`` snapshot and
            # completed the exact same migration first.  Treat that specific
            # race as success; any missing/different checksum remains a real
            # migration error and is never hidden.
            completed_elsewhere = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version=?", (version,)
            ).fetchone()
            if completed_elsewhere and str(completed_elsewhere[0]) == digest:
                applied[version] = digest
                continue
            raise
        completed.append(version)
        applied[version] = digest
    for table, columns in LEGACY_COLUMNS.items():
        ensure_columns(conn, table, columns)
    ensure_legacy_indexes(conn)
    fts_available = ensure_optional_fts(conn, refresh=bool(completed) or repair_fts)
    conn.commit()
    return {"applied": completed, "fts_available": fts_available}


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply Meta Memory database migrations.")
    parser.add_argument("--db", required=True, help="Path to the SQLite database")
    parser.add_argument("--repair-fts", action="store_true", help="Explicitly rebuild optional FTS indexes")
    args = parser.parse_args()
    db = Path(args.db).expanduser().resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    result = run_migrations(conn, repair_fts=args.repair_fts)
    conn.close()
    print({"status": "ok", "db": str(db), **result})


if __name__ == "__main__":
    main()
