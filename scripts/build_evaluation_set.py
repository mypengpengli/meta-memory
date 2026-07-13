#!/usr/bin/env python3
"""Create a privacy-local evaluation corpus from real session evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from _common import DEFAULT_STORE_HELP, emit, open_db, store_root


def build_set(root, *, subject_id: str, limit: int = 100) -> list[dict[str, object]]:
    conn = open_db(root)
    rows = conn.execute("SELECT session_id, id, content FROM session_messages WHERE role='user' AND session_id IN (SELECT session_id FROM sessions WHERE subject_id=?) ORDER BY id DESC LIMIT ?", (subject_id, limit)).fetchall()
    conn.close()
    return [{"session_id": str(row[0]), "message_id": int(row[1]), "query": str(row[2]), "source": "session"} for row in rows if str(row[2]).strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a local retrieval/evolution evaluation set."); parser.add_argument("--store", help=DEFAULT_STORE_HELP); parser.add_argument("--subject-id", required=True); parser.add_argument("--out-file", required=True); parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(); rows = build_set(store_root(args.store), subject_id=args.subject_id, limit=args.limit); Path(args.out_file).write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""), encoding="utf-8"); emit({"status": "ok", "count": len(rows), "out_file": args.out_file})


if __name__ == "__main__": main()
