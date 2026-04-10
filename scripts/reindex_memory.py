#!/usr/bin/env python3
from __future__ import annotations

from _common import (
    emit,
    first_heading,
    markdown_files,
    open_db,
    parse_args,
    parse_frontmatter,
    store_root,
)


def main() -> None:
    args = parse_args("Reindex curated memory notes into SQLite.")
    root = store_root(args.store)
    conn = open_db(root)
    files = markdown_files(
        [
            root / "fixed",
            root / "topics",
            root / "projects",
            root / "sessions",
            root / "candidates",
        ]
    )
    indexed = 0
    for path in files:
        meta = parse_frontmatter(path)
        conn.execute(
            """
            INSERT INTO documents(path, scope, doc_type, title, topic, tags, status, mtime)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                scope=excluded.scope,
                doc_type=excluded.doc_type,
                title=excluded.title,
                topic=excluded.topic,
                tags=excluded.tags,
                status=excluded.status,
                mtime=excluded.mtime
            """,
            (
                str(path),
                meta.get("scope", ""),
                meta.get("type", ""),
                first_heading(path),
                meta.get("topic", ""),
                str(meta.get("tags", "")),
                meta.get("status", ""),
                path.stat().st_mtime,
            ),
        )
        indexed += 1
    conn.commit()
    conn.close()
    emit({"status": "ok", "indexed": indexed, "store": str(root)})


if __name__ == "__main__":
    main()
