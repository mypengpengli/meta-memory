#!/usr/bin/env python3
"""Optionally index chunks with externally supplied embeddings."""
from __future__ import annotations

import argparse
import json

from _common import DEFAULT_STORE_HELP, emit, open_db, sha256_text, store_root
from llm_client import embed


def main() -> None:
    parser = argparse.ArgumentParser(description="Build optional chunk embeddings through META_MEMORY_EMBEDDINGS_COMMAND.")
    parser.add_argument("--store", help=DEFAULT_STORE_HELP)
    parser.add_argument("--model", default="external")
    parser.add_argument("--subject-id")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = store_root(args.store)
    conn = open_db(root)
    clauses = []
    params: list[object] = []
    if args.subject_id:
        clauses.append("d.subject_id=?")
        params.append(args.subject_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT c.id, c.content, c.content_hash, d.subject_id FROM chunks c JOIN documents d ON d.path=c.doc_path {where} ORDER BY c.id", tuple(params)).fetchall()
    indexed = 0
    skipped = 0
    for offset in range(0, len(rows), max(1, args.batch_size)):
        batch = rows[offset : offset + max(1, args.batch_size)]
        needed = []
        for row in batch:
            existing = conn.execute("SELECT content_hash FROM embeddings WHERE node_type='chunk' AND node_id=? AND model=?", (str(row[0]), args.model)).fetchone()
            if existing and str(existing[0]) == str(row[2]):
                skipped += 1
            else:
                needed.append(row)
        if not needed:
            continue
        vectors = embed([str(row[1]) for row in needed])
        if vectors is None:
            conn.close()
            emit({"status": "skipped", "reason": "META_MEMORY_EMBEDDINGS_COMMAND is not configured", "indexed": indexed, "skipped": skipped})
            return
        if len(vectors) != len(needed):
            raise SystemExit("Embedding provider returned a different number of vectors")
        if not args.dry_run:
            for row, vector in zip(needed, vectors):
                conn.execute("INSERT INTO embeddings(node_type, node_id, subject_id, model, content_hash, vector_json) VALUES('chunk', ?, ?, ?, ?, ?) ON CONFLICT(node_type,node_id,model) DO UPDATE SET subject_id=excluded.subject_id, content_hash=excluded.content_hash, vector_json=excluded.vector_json, updated_at=CURRENT_TIMESTAMP", (str(row[0]), str(row[3]), args.model, str(row[2]), json.dumps(vector)))
        indexed += len(needed)
    if not args.dry_run:
        conn.commit()
    conn.close()
    emit({"status": "ok", "indexed": indexed, "skipped": skipped, "model": args.model, "dry_run": args.dry_run})


if __name__ == "__main__":
    main()
