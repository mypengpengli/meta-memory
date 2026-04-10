from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Iterable


DEFAULT_DIRS = [
    "fixed",
    "topics",
    "projects",
    "sessions",
    "candidates",
    "archive/raw",
    "archive/imports",
    "db",
]


def parse_args(description: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--store", required=True, help="Path to the external memory-data root")
    return parser.parse_args()


def store_root(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def ensure_default_dirs(root: Path) -> None:
    for relative in DEFAULT_DIRS:
        (root / relative).mkdir(parents=True, exist_ok=True)


def db_path(root: Path) -> Path:
    return root / "db" / "memory_index.sqlite"


def open_db(root: Path) -> sqlite3.Connection:
    ensure_default_dirs(root)
    conn = sqlite3.connect(db_path(root))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            scope TEXT,
            doc_type TEXT,
            title TEXT,
            topic TEXT,
            tags TEXT,
            status TEXT,
            mtime REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scores (
            path TEXT PRIMARY KEY,
            hit_count INTEGER DEFAULT 0,
            confidence REAL DEFAULT 0.0,
            rank_score REAL DEFAULT 0.0,
            last_hit_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS retrieval_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            used_paths TEXT
        )
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


def parse_frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}
    block = text[4:end].splitlines()
    data: dict[str, object] = {}
    for line in block:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def first_heading(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
