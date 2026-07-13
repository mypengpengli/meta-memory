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


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def ensure_optional_fts(conn: sqlite3.Connection) -> bool:
    try:
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


def run_migrations(conn: sqlite3.Connection) -> dict[str, object]:
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
                raise RuntimeError(f"Migration checksum mismatch for {path.name}; do not edit an applied migration.")
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
            raise
        completed.append(version)
    for table, columns in LEGACY_COLUMNS.items():
        ensure_columns(conn, table, columns)
    ensure_legacy_indexes(conn)
    fts_available = ensure_optional_fts(conn)
    conn.commit()
    return {"applied": completed, "fts_available": fts_available}


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply Meta Memory database migrations.")
    parser.add_argument("--db", required=True, help="Path to the SQLite database")
    args = parser.parse_args()
    db = Path(args.db).expanduser().resolve()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    result = run_migrations(conn)
    conn.close()
    print({"status": "ok", "db": str(db), **result})


if __name__ == "__main__":
    main()
