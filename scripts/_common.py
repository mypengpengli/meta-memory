from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import sqlite3
from pathlib import Path
from typing import Iterable

DEFAULT_STORE_DIRNAME = "memory-data"
DEFAULT_STORE_HELP = f"Path to the memory-data root; defaults to ./{DEFAULT_STORE_DIRNAME} under the skill root"


DEFAULT_DIRS = [
    "profile",
    "states",
    "events",
    "relationships",
    "goals",
    "domains",
    "sessions",
    "candidates",
    "archive/raw",
    "archive/imports",
    "db",
]

REPORTABLE_LAYOUT = [
    "profile",
    "states",
    "events",
    "relationships",
    "goals",
    "domains",
    "sessions",
    "candidates",
    "archive/raw",
    "archive/imports",
]

INDEXED_DIRS = [
    "profile",
    "states",
    "events",
    "relationships",
    "goals",
    "domains",
    "sessions",
    "candidates",
]

LEGACY_INDEXED_DIRS = [
    "fixed",
    "topics",
    "projects",
]

DOCUMENT_COLUMNS = {
    "title": "TEXT",
    "subject_id": "TEXT",
    "subject_name": "TEXT",
    "memory_kind": "TEXT",
    "domain": "TEXT",
    "topic": "TEXT",
    "tags": "TEXT",
    "summary": "TEXT",
    "confidence": "REAL",
    "status": "TEXT",
    "source": "TEXT",
    "start_at": "TEXT",
    "end_at": "TEXT",
    "related_people": "TEXT",
    "related_events": "TEXT",
    "related_topics": "TEXT",
    "related_sources": "TEXT",
    "supersedes": "TEXT",
    "replaced_by": "TEXT",
    "mtime": "REAL",
}

SCORE_COLUMNS = {
    "hit_count": "INTEGER DEFAULT 0",
    "confidence": "REAL DEFAULT 0.0",
    "rank_score": "REAL DEFAULT 0.0",
    "last_hit_at": "TEXT",
}

RAW_EVENT_COLUMNS = {
    "subject_id": "TEXT",
    "subject_name": "TEXT",
    "session_id": "TEXT",
    "source_type": "TEXT",
    "source_ref": "TEXT",
    "content": "TEXT",
    "content_hash": "TEXT",
    "topic_hint": "TEXT",
    "domain_hint": "TEXT",
    "event_time": "TEXT",
    "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
    "processed_state": "TEXT DEFAULT 'pending'",
    "processed_at": "TEXT",
    "batch_id": "TEXT",
    "classifier_kind": "TEXT",
    "classifier_domain": "TEXT",
    "target_memory_kind": "TEXT",
    "target_memory_path": "TEXT",
    "note": "TEXT",
}

CURSOR_COLUMNS = {
    "last_processed_event_id": "INTEGER DEFAULT 0",
    "last_organized_at": "TEXT",
    "last_heartbeat_at": "TEXT",
}

MEMORY_SOURCE_COLUMNS = {
    "memory_path": "TEXT",
    "raw_event_id": "INTEGER",
    "link_role": "TEXT",
    "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
}


def parse_args(description: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    return parser.parse_args()


def skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_store_root() -> Path:
    return (skill_root() / DEFAULT_STORE_DIRNAME).resolve()


def store_root(raw: str | None) -> Path:
    text = str(raw or "").strip()
    if text:
        return Path(text).expanduser().resolve()
    return default_store_root()


def ensure_default_dirs(root: Path) -> None:
    for relative in DEFAULT_DIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)


def ensure_store_ready(root: Path) -> dict[str, object]:
    existing_layout = {relative: (root / relative).exists() for relative in REPORTABLE_LAYOUT}
    db_file_exists = db_path(root).exists()
    conn = open_db(root)
    conn.close()

    created = [relative for relative in REPORTABLE_LAYOUT if not existing_layout[relative]]
    if not db_file_exists:
        created.append("db/memory_index.sqlite")

    return {
        "store": str(root),
        "initialized": bool(created),
        "created": created,
    }


def indexed_roots(root: Path) -> list[Path]:
    results: list[Path] = []
    for relative in INDEXED_DIRS + LEGACY_INDEXED_DIRS:
        path = root / relative
        if path.exists() and path not in results:
            results.append(path)
    return results


def db_path(root: Path) -> Path:
    return root / "db" / "memory_index.sqlite"


def ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, sql_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def open_db(root: Path) -> sqlite3.Connection:
    ensure_default_dirs(root)
    conn = sqlite3.connect(db_path(root))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL
        )
        """
    )
    ensure_columns(conn, "documents", DOCUMENT_COLUMNS)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scores (
            path TEXT PRIMARY KEY
        )
        """
    )
    ensure_columns(conn, "scores", SCORE_COLUMNS)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS retrieval_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            used_paths TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        )
        """
    )
    ensure_columns(conn, "raw_events", RAW_EVENT_COLUMNS)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS maintenance_cursor (
            subject_id TEXT PRIMARY KEY
        )
        """
    )
    ensure_columns(conn, "maintenance_cursor", CURSOR_COLUMNS)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        )
        """
    )
    ensure_columns(conn, "memory_sources", MEMORY_SOURCE_COLUMNS)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_raw_events_subject_state
        ON raw_events(subject_id, processed_state, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_raw_events_subject_hash
        ON raw_events(subject_id, content_hash)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_raw_events_created_at
        ON raw_events(created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_raw_events_subject_event_time
        ON raw_events(subject_id, event_time, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_raw_events_topic_domain
        ON raw_events(topic_hint, domain_hint)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_sources_raw_event
        ON memory_sources(raw_event_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_sources_memory_path
        ON memory_sources(memory_path)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_sources_unique_link
        ON memory_sources(memory_path, raw_event_id, link_role)
        """
    )
    conn.commit()
    return conn


def markdown_files(paths: Iterable[Path]) -> list[Path]:
    results: list[Path] = []
    for base in paths:
        if base.exists():
            results.extend(sorted(base.rglob("*.md")))
    return results


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def parse_scalar(value: str) -> object:
    stripped = value.strip()
    if stripped == "":
        return ""
    if stripped in ('""', "''"):
        return ""
    if stripped.lower() == "true":
        return True
    if stripped.lower() == "false":
        return False
    if stripped == "[]":
        return []
    if stripped.startswith("[") and stripped.endswith("]"):
        try:
            parsed = ast.literal_eval(stripped)
            if isinstance(parsed, list):
                return parsed
        except (SyntaxError, ValueError):
            inner = stripped[1:-1].strip()
            if not inner:
                return []
            return [item.strip().strip('"').strip("'") for item in inner.split(",")]
    try:
        number = float(stripped)
    except ValueError:
        return stripped.strip('"').strip("'")
    if math.isfinite(number) and number.is_integer():
        return int(number)
    return number


def split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    data: dict[str, object] = {}
    current_key: str | None = None
    body_start = 0

    for idx in range(1, len(lines)):
        line = lines[idx]
        stripped = line.strip()
        if stripped == "---":
            body_start = idx + 1
            break
        if not stripped:
            continue
        if stripped.startswith("- ") and current_key:
            current = data.setdefault(current_key, [])
            if isinstance(current, list):
                current.append(parse_scalar(stripped[2:]))
            continue
        if ":" not in line:
            current_key = None
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        raw_value = value.strip()
        if raw_value == "":
            data[key] = []
            current_key = key
        else:
            parsed = parse_scalar(value)
            data[key] = parsed
            current_key = key if isinstance(parsed, list) else None

    if body_start == 0:
        return {}, text

    body = "\n".join(lines[body_start:]).lstrip("\n")
    return data, body


def parse_frontmatter(path: Path) -> dict[str, object]:
    meta, _ = split_frontmatter(read_text(path))
    return meta


def dump_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return '""'
    text = str(value)
    if text == "":
        return '""'
    if any(ch in text for ch in [":", "#", "[", "]", "{", "}", ","]):
        return json.dumps(text, ensure_ascii=False)
    return text


def compose_markdown(meta: dict[str, object], body: str) -> str:
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {dump_scalar(value)}")
    lines.append("---")
    lines.append("")
    if body:
        lines.append(body.rstrip())
        lines.append("")
    return "\n".join(lines)


def first_heading(path: Path) -> str:
    _, body = split_frontmatter(read_text(path))
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def first_summary_line(path: Path) -> str:
    _, body = split_frontmatter(read_text(path))
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        return stripped.lstrip("- ").strip()
    return first_heading(path)


def json_text(value: object) -> str:
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    if value in (None, ""):
        return ""
    return str(value)


def as_float(value: object, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
