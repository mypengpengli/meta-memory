#!/usr/bin/env python3
from __future__ import annotations

from _common import ensure_default_dirs, emit, open_db, parse_args, store_root


def main() -> None:
    args = parse_args("Initialize a layered memory-data store.")
    root = store_root(args.store)
    ensure_default_dirs(root)
    conn = open_db(root)
    conn.close()
    emit(
        {
            "status": "ok",
            "store": str(root),
            "created": [
                "fixed",
                "topics",
                "projects",
                "sessions",
                "candidates",
                "archive/raw",
                "archive/imports",
                "db/memory_index.sqlite",
            ],
        }
    )


if __name__ == "__main__":
    main()
