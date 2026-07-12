#!/usr/bin/env python3
"""Conservatively attach V2 claim records to existing Markdown stores."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from _common import DEFAULT_STORE_HELP, compose_markdown, emit, open_db, read_text, sha256_text, split_frontmatter, store_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate a V1 Meta Memory store to V2 metadata and claims.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out-file")
    return parser.parse_args()


def atomic_write(path: Path, text: str) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except Exception:
        Path(name).unlink(missing_ok=True)
        raise


def main() -> None:
    args = parse_args()
    root = store_root(args.store)
    if not args.dry_run:
        subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "reindex_memory.py"), "--store", str(root)], check=True, capture_output=True, text=True)
        subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "build_views.py"), "--store", str(root)], check=True, capture_output=True, text=True)
    conn = open_db(root)
    docs = conn.execute("SELECT path, subject_id, subject_name, memory_kind, topic, title, summary, confidence, importance, status FROM documents").fetchall()
    manifest = {"files_before": len(docs), "files_after": len(docs), "raw_events_before": conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0], "raw_events_after": 0, "claims_created": 0, "missing_sources": 0, "errors": []}
    for row in docs:
        path, subject_id, subject_name, kind, topic, title, summary, confidence, importance, status = row
        source_rows = conn.execute("SELECT raw_event_id FROM memory_sources WHERE memory_path=?", (path,)).fetchall()
        source_ids = [int(item[0]) for item in source_rows]
        if not source_ids:
            manifest["missing_sources"] += 1
        existing = conn.execute("SELECT id FROM claims WHERE memory_path=?", (path,)).fetchone()
        claim_id = str(existing[0]) if existing else str(uuid.uuid4())
        if not existing and not args.dry_run:
            content_hash = sha256_text(str(summary or title))
            conn.execute(
                """INSERT INTO claims(id, subject_id, subject_name, memory_kind, topic, title, content, content_hash,
                   status, verification_state, confidence, importance, sensitivity, valid_from, support_count, memory_path)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'legacy', ?, ?, 'normal', '', ?, ?)""",
                (claim_id, subject_id, subject_name, kind or "candidate", topic or "legacy", title or Path(path).stem, summary or title or "", content_hash, status or "active", confidence or 0.5, importance or 0.5, len(source_ids), path),
            )
            for event_id in source_ids:
                conn.execute("INSERT OR IGNORE INTO claim_sources(claim_id, raw_event_id, source_role) VALUES(?, ?, 'legacy')", (claim_id, event_id))
            manifest["claims_created"] += 1
        file_path = Path(path)
        if file_path.exists() and not args.dry_run:
            meta, body = split_frontmatter(read_text(file_path))
            meta["schema_version"] = 2
            meta["memory_id"] = claim_id
            meta.setdefault("source_event_ids", source_ids)
            atomic_write(file_path, compose_markdown(meta, body))
    if not args.dry_run:
        conn.commit()
    manifest["raw_events_after"] = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
    conn.close()
    if not args.dry_run:
        subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "reindex_memory.py"), "--store", str(root)], check=True, capture_output=True, text=True)
    result = {"status": "ok", "dry_run": args.dry_run, "manifest": manifest}
    if args.out_file:
        Path(args.out_file).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(result)


if __name__ == "__main__":
    main()
