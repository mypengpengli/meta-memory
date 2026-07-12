#!/usr/bin/env python3
"""Audit candidate metadata without silently rewriting evidence-bearing files."""
from __future__ import annotations

from _common import emit, open_db, parse_args, store_root


REQUIRED = {"subject_id", "memory_kind", "topic", "confidence", "status", "memory_id", "schema_version"}


def main() -> None:
    args = parse_args("Report candidate pages that need a validated REFINE plan; never rewrite them directly.")
    root = store_root(args.store)
    conn = open_db(root)
    rows = conn.execute("SELECT id, memory_path, topic, confidence, status FROM claims WHERE memory_kind='candidate' ORDER BY created_at").fetchall()
    conn.close()
    findings = [{"claim_id": row[0], "path": row[1], "topic": row[2], "confidence": row[3], "status": row[4], "suggestion": "Use a REFINE plan if metadata or content needs correction."} for row in rows]
    emit({"status": "ok", "normalized": 0, "audited": len(findings), "findings": findings, "store": str(root)})


if __name__ == "__main__":
    main()
